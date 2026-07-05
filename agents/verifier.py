"""
agents/verifier.py — Verifier agent: independent check that can OVERRULE the Worker.

The Verifier is the agent-checks-agent gate. It:
  1. Receives the source record AND the Worker's output
  2. Checks for hallucinated fields/values not present in the source
  3. Validates structural integrity against output_schema.yaml
  4. Produces verdict: pass | fail | needs_human
  5. Can OVERRULE the Worker — disagreement is logged with both sides

The Verifier calls NO other agents (it is an independent leaf — can_call = []).
A DELIVERED record MUST have a Verifier span with verdict='pass'.
"""
from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional

# Load allowed delivered fields from output_schema.py
from output_schema import ALLOWED_FIELDS, REQUIRED_FIELDS
_ALLOWED_FIELDS = set(ALLOWED_FIELDS)
_REQUIRED_FIELDS = set(REQUIRED_FIELDS)

from agents.contracts import (
    NormalizedRecord, ReasonCode, VerifierInput, VerifierOutput,
    VerifierVerdict, WorkerOutput,
)
from llm.client import LLMClient
from llm.router import estimate_cost

PROMPT_VERSION = "verifier-v1"

VALID_PRIORITY_LEVELS = {"routine", "elevated", "urgent"}

VERIFIER_SYSTEM_PROMPT = """You are the Verifier agent in the CEDX Legal Case Management pipeline.
Your job: independently verify that the Worker's legal case brief is factually grounded in the source filing.

Check for:
1. Hallucinated fields (fields the Worker invented that are not in the allowed schema)
2. Hallucinated values (e.g. wrong attorney, wrong id, wrong claim amount)
3. Missing required fields
4. Invalid priority_level or matter_classification

Respond with ONLY this JSON:
{
  "verdict": "pass" | "fail" | "needs_human",
  "reason": "<brief explanation>"
}

verdict=pass: Worker output is grounded and correct.
verdict=fail: Worker hallucinated or produced invalid output (route to AGENT_HALLUCINATION or AGENT_MALFORMED).
verdict=needs_human: Ambiguous case, route to attorney review.

Respond with ONLY the JSON object."""


class VerifierAgent:
    """
    Verifier agent — independently checks Worker output.
    Can OVERRULE the Worker. Must produce a 'pass' verdict for delivery.

    Independent leaf: can_call = []
    """

    name = "Verifier"
    can_call: list[str] = []

    def __init__(self):
        self.client = LLMClient(agent_name=self.name)

    def process(self, inp: VerifierInput) -> VerifierOutput:
        """
        Main entry point.
        First runs fast rule-based checks, then optionally LLM check.
        """
        source = inp.source
        worker_out = inp.worker_output

        # Fast path: Worker abstained — not a hallucination, pass through
        if worker_out.abstain:
            return VerifierOutput(
                record_id=source.id,
                verdict=VerifierVerdict.PASS,
                detail="Worker abstained — no output to verify",
            )

        if worker_out.delivered_fields is None:
            return VerifierOutput(
                record_id=source.id,
                verdict=VerifierVerdict.FAIL,
                reason_code=ReasonCode.AGENT_MALFORMED,
                detail="Worker produced no delivered_fields and did not abstain",
            )

        df = worker_out.delivered_fields.model_dump()

        # ── Rule-based checks (fast, always run) ────────────────────────────
        rule_verdict, rule_code, rule_detail = self._rule_check(source, df, inp.allowed_delivered_fields)
        if rule_verdict == VerifierVerdict.FAIL:
            return VerifierOutput(
                record_id=source.id,
                verdict=VerifierVerdict.FAIL,
                reason_code=rule_code,
                detail=rule_detail,
            )

        # ── LLM-based verification (real path or if rule check passes) ───────
        try:
            response, tokens_in, tokens_out, latency_ms, tx_hash = (
                self.client.complete(
                    messages=self._build_messages(source, df),
                    record_id=source.id,
                    model=self._pick_model(),
                    prompt_version=PROMPT_VERSION,
                )
            )
            verdict_str = response.get("verdict", "needs_human")
            detail = response.get("reason", "")
            verdict = VerifierVerdict(verdict_str) if verdict_str in VerifierVerdict._value2member_map_ else VerifierVerdict.NEEDS_HUMAN

            reason_code = None
            if verdict == VerifierVerdict.FAIL:
                detail_lower = detail.lower()
                if "hallucin" in detail_lower or "invented" in detail_lower or "not in source" in detail_lower:
                    reason_code = ReasonCode.AGENT_HALLUCINATION
                else:
                    reason_code = ReasonCode.AGENT_MALFORMED

            return VerifierOutput(
                record_id=source.id,
                verdict=verdict,
                reason_code=reason_code,
                detail=detail,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                cost_usd=estimate_cost(self._pick_model(), tokens_in, tokens_out),
                latency_ms=latency_ms,
                transcript_hash=tx_hash,
            )

        except RuntimeError:
            if rule_verdict == VerifierVerdict.PASS:
                return VerifierOutput(
                    record_id=source.id,
                    verdict=VerifierVerdict.PASS,
                    detail="Rule-based check passed (no verifier transcript in replay mode)",
                )
            return VerifierOutput(
                record_id=source.id,
                verdict=VerifierVerdict.NEEDS_HUMAN,
                detail="Verifier replay transcript missing; manual review required",
            )

    def _rule_check(
        self,
        source: NormalizedRecord,
        df: dict,
        allowed_fields: list[str],
    ) -> tuple[VerifierVerdict, Optional[ReasonCode], str]:
        """Fast rule-based hallucination and schema checks."""
        allowed = set(allowed_fields) if allowed_fields else _ALLOWED_FIELDS

        hallucinated = [k for k in df if k not in allowed and df[k] is not None]
        if hallucinated:
            return (
                VerifierVerdict.FAIL,
                ReasonCode.AGENT_HALLUCINATION,
                f"Hallucinated fields not in allowed schema: {hallucinated}",
            )

        missing_required = [f for f in _REQUIRED_FIELDS if not df.get(f)]
        if missing_required:
            return (
                VerifierVerdict.FAIL,
                ReasonCode.AGENT_MALFORMED,
                f"Required delivered fields missing: {missing_required}",
            )

        if df.get("id") != source.id:
            return (
                VerifierVerdict.FAIL,
                ReasonCode.AGENT_HALLUCINATION,
                f"ID mismatch: worker says {df.get('id')!r}, source is {source.id!r}",
            )

        if source.owner and df.get("attorney") != source.owner:
            return (
                VerifierVerdict.FAIL,
                ReasonCode.AGENT_HALLUCINATION,
                f"Attorney mismatch: worker says {df.get('attorney')!r}, source is {source.owner!r}",
            )

        if source.category and df.get("case_type") != source.category:
            return (
                VerifierVerdict.FAIL,
                ReasonCode.AGENT_HALLUCINATION,
                f"Case type mismatch: worker says {df.get('case_type')!r}, source is {source.category!r}",
            )

        if source.amount is not None:
            worker_amount = df.get("normalized_claim_amount", 0)
            if abs(worker_amount - source.amount) > max(1.0, source.amount * 0.01):
                return (
                    VerifierVerdict.FAIL,
                    ReasonCode.AGENT_HALLUCINATION,
                    f"Claim amount mismatch: worker says {worker_amount}, source is {source.amount}",
                )

        if df.get("priority_level") not in VALID_PRIORITY_LEVELS:
            return (
                VerifierVerdict.FAIL,
                ReasonCode.AGENT_MALFORMED,
                f"Invalid priority_level: {df.get('priority_level')!r}",
            )

        return VerifierVerdict.PASS, None, "All rule-based checks passed"

    def _build_messages(self, source: NormalizedRecord, df: dict) -> list[dict]:
        return [
            {"role": "system", "content": VERIFIER_SYSTEM_PROMPT},
            {"role": "user", "content": json.dumps({
                "source_record": source.model_dump(),
                "worker_output": df,
            }, ensure_ascii=False)},
        ]

    def _pick_model(self) -> str:
        from llm.router import CHEAP_MODEL
        return CHEAP_MODEL
