"""
agents/worker.py — Worker agent: LLM-heavy Assembly draft.

The Worker is a separable, independently-testable unit. It:
  1. Receives a NormalizedRecord + chosen model from the Orchestrator
  2. Calls the LLM (real or replayed) with a structured prompt
  3. Returns a WorkerOutput with DeliveredFields or abstain=True
  4. Records model, tokens, cost, transcript_hash in the output for tracing

The Worker calls NO other agents (it is a leaf node). Its can_call = [].
"""
from __future__ import annotations

import json
import os
import time
from typing import Optional

from agents.contracts import (
    DeliveredFields, NormalizedRecord, ReasonCode, TraceStatus,
    WorkerInput, WorkerOutput,
)
from llm.client import LLMClient
from llm.router import estimate_cost

PROMPT_VERSION = "worker-v1"
BRAND = "CEDX Legal Services - Case Management Division"
PIPELINE_VERSION = "1.0.0"

SYSTEM_PROMPT = """You are the Worker agent in the CEDX Legal Case Management pipeline.
Your job: given a normalized legal case filing, produce a structured case analysis brief.

You must respond with ONLY valid JSON matching this exact schema:
{
  "id": "<case id>",
  "attorney": "<assigned attorney>",
  "case_type": "<case_type>",
  "normalized_claim_amount": <number>,
  "matter_classification": "<human-readable label>",
  "priority_level": "routine" | "elevated" | "urgent",
  "recommended_strategy": "<one sentence>",
  "case_summary": "<2-3 sentence analysis>",
  "law_firm_brand": "CEDX Legal Services - Case Management Division",
  "pipeline_version": "1.0.0",
  "generated_at": "<ISO timestamp>"
}

Priority level rules:
- routine: claim_amount < 10,000
- elevated: claim_amount 10,000-49,999 OR case_type is REVIEW
- urgent:   claim_amount >= 50,000

Matter classifications (map source category to legal label):
- ONBOARDING -> "New Matter Intake"
- INTAKE     -> "New Matter Intake"
- RENEWAL    -> "Retainer Renewal Review"
- REVIEW     -> "Case Status Review"
- REPORT     -> "Litigation Status Report"

The case_type field in your output must match the source category exactly.
The attorney field must match the source owner field exactly.
The normalized_claim_amount must match the source amount exactly.

CRITICAL: Do NOT invent any fields. Do NOT fabricate case facts, citations, or legal arguments not in the source.
If you are not confident in the output (case facts unclear, contradictory information), respond with:
{"abstain": true, "reason": "<brief explanation>"}

Respond with ONLY the JSON object. No prose, no markdown fences."""


class WorkerAgent:
    """
    Worker agent — produces structured branded output from a normalized record.

    This is an independent, testable unit. Give it a WorkerInput, get WorkerOutput.
    It never calls other agents (can_call = []).
    """

    name = "Worker"
    can_call: list[str] = []

    def __init__(self):
        self.client = LLMClient(agent_name=self.name)

    def process(self, inp: WorkerInput) -> WorkerOutput:
        """
        Main entry point. Calls LLM (real or replay), handles abstain/repair/retry.
        """
        record = inp.record
        model = inp.model
        retries = 0
        max_retries = 2

        while retries <= max_retries:
            try:
                response, tokens_in, tokens_out, latency_ms, tx_hash = (
                    self.client.complete(
                        messages=self._build_messages(record),
                        record_id=record.id,
                        model=model,
                        prompt_version=PROMPT_VERSION,
                        delivered_fields=None,  # set after parse
                    )
                )
            except RuntimeError as e:
                # No transcript for this record (replay mode, unrecognized record)
                # Route as abstain so it gets LOW_CONFIDENCE handling
                return WorkerOutput(
                    record_id=record.id,
                    abstain=True,
                    abstain_reason=f"No replay transcript available: {e}",
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    retries=retries,
                )

            cost_usd = estimate_cost(model, tokens_in, tokens_out)

            # Check for abstain signal
            if response.get("abstain") is True:
                return WorkerOutput(
                    record_id=record.id,
                    abstain=True,
                    abstain_reason=response.get("reason", "Worker abstained"),
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    retries=retries,
                    transcript_hash=tx_hash,
                )

            # Try to parse the response into DeliveredFields
            parsed = self._parse_response(response, record)
            if parsed is not None:
                return WorkerOutput(
                    record_id=record.id,
                    abstain=False,
                    delivered_fields=parsed,
                    model=model,
                    prompt_version=PROMPT_VERSION,
                    tokens_in=tokens_in,
                    tokens_out=tokens_out,
                    cost_usd=cost_usd,
                    latency_ms=latency_ms,
                    retries=retries,
                    transcript_hash=tx_hash,
                )

            # Parse failed → retry with repair hint
            retries += 1
            if retries > max_retries:
                break

        # Exceeded retries → abstain (AGENT_MALFORMED will be caught by Verifier)
        return WorkerOutput(
            record_id=record.id,
            abstain=True,
            abstain_reason="Max retries exceeded — malformed response",
            model=model,
            prompt_version=PROMPT_VERSION,
            retries=retries,
        )

    def _build_messages(self, record: NormalizedRecord) -> list[dict]:
        """Build the messages list for the LLM call."""
        user_content = json.dumps({
            "id": record.id,
            "owner": record.owner,
            "deadline": record.deadline,
            "amount": record.amount,
            "category": record.category,
            "notes": record.notes or "",
            "version": record.version,
        }, ensure_ascii=False)

        return [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": f"Process this record:\n{user_content}"},
        ]

    def _parse_response(
        self, response: dict, record: NormalizedRecord
    ) -> Optional[DeliveredFields]:
        """
        Try to parse LLM response into DeliveredFields.
        Returns None if the response is malformed.
        """
        try:
            # Set generated_at if missing
            if "generated_at" not in response or not response.get("generated_at"):
                response["generated_at"] = time.strftime(
                    "%Y-%m-%dT%H:%M:%SZ", time.gmtime()
                )
            # Force brand + pipeline_version
            response["law_firm_brand"] = BRAND
            response["pipeline_version"] = PIPELINE_VERSION
            response.pop("brand", None)

            # Only keep allowed fields (extra fields will be caught by Verifier)
            return DeliveredFields(**{
                k: v for k, v in response.items()
                if k in DeliveredFields.model_fields
            })
        except Exception:
            return None
