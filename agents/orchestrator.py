"""
agents/orchestrator.py — Orchestrator: owns the run, routes records, enforces budgets.

The Orchestrator:
  1. Receives all normalized records
  2. Decides which records go to Assembly (Worker → Verifier) vs exception queue
  3. Enforces per-record step + cost budgets
  4. Kills agent runs that exceed step limit (AGENT_LOOP)
  5. Delegates business logic to Worker and Verifier — never inlines it

The Orchestrator may call: Worker, Verifier.
It does NOT contain business logic (that lives in pipeline/orchestration.py for detectors,
agents/worker.py for drafting, agents/verifier.py for checking).
"""
from __future__ import annotations

import os
import time
from typing import Optional

from agents.contracts import (
    AgentSpan, CLASS_A_CODES, NormalizedRecord, ReasonCode, ReasonClass,
    RecordRouting, TraceStatus, VerifierVerdict, WorkerInput, WorkerOutput,
    OrchestratorInput, OrchestratorOutput, VerifierInput,
)
from agents.worker import WorkerAgent
from agents.verifier import VerifierAgent
from llm.router import select_model, estimate_cost

MAX_COST_USD_PER_RECORD = float(os.getenv("MAX_COST_USD_PER_RECORD", "0.05"))
MAX_STEPS_PER_RECORD = int(os.getenv("MAX_STEPS_PER_RECORD", "5"))


class OrchestratorAgent:
    """
    Orchestrator — owns the pipeline run.

    Accepts OrchestratorInput, returns OrchestratorOutput.
    Calls Worker and Verifier agents via their typed contracts.
    Cannot be confused with the Worker or Verifier — it delegates, never implements.
    """

    name = "Orchestrator"
    can_call = ["Worker", "Verifier"]

    def __init__(self):
        self.worker = WorkerAgent()
        self.verifier = VerifierAgent()
        self._last_worker_outputs: dict[str, WorkerOutput] = {}  # record_id → last approved WorkerOutput

    def run(self, inp: OrchestratorInput) -> tuple[OrchestratorOutput, dict[str, list[AgentSpan]]]:
        """
        Process all records. Returns (OrchestratorOutput, per_record_traces).
        per_record_traces: record_id → [AgentSpan, ...]
        """
        routings: list[RecordRouting] = []
        traces: dict[str, list[AgentSpan]] = {}
        total_cost = 0.0
        total_steps = 0

        for record in inp.records:
            routing, spans = self._process_record(record, inp)
            routings.append(routing)
            traces[record.id] = spans
            total_cost += routing.cost_usd
            total_steps += routing.steps_used

        return (
            OrchestratorOutput(
                run_id=inp.run_id,
                routings=routings,
                total_cost_usd=total_cost,
                total_steps=total_steps,
            ),
            traces,
        )

    def _process_record(
        self,
        record: NormalizedRecord,
        inp: OrchestratorInput,
    ) -> tuple[RecordRouting, list[AgentSpan]]:
        """Route one record: assemble or exception."""
        spans: list[AgentSpan] = []
        steps = 0
        total_cost = 0.0

        # Start Orchestrator span
        spans.append(AgentSpan(
            agent=self.name,
            status=TraceStatus.OK,
            detail=f"Routing record {record.id}",
        ))
        steps += 1

        # ── Attempt Worker + Verifier (with retry on verifier rejection) ──
        escalate = False
        max_worker_attempts = 2

        for attempt in range(max_worker_attempts + 1):
            # Budget check before each attempt
            if total_cost >= inp.max_cost_usd_per_record:
                routing, budget_span = self._budget_exceeded(record, steps, total_cost)
                spans.append(budget_span)
                return routing, spans

            if steps >= inp.max_steps_per_record:
                routing, loop_span = self._agent_loop(record, steps, total_cost)
                spans.append(loop_span)
                return routing, spans

            # ── Worker call ────────────────────────────────────────────────
            model = select_model(record, retries=attempt, escalate=escalate)
            worker_inp = WorkerInput(
                record=record,
                model=model,
                replay_llm=inp.replay_llm,
            )
            t0 = time.time()
            worker_out = self.worker.process(worker_inp)
            worker_latency = (time.time() - t0) * 1000
            steps += 1
            total_cost += worker_out.cost_usd

            worker_span = AgentSpan(
                agent=self.worker.name,
                model=worker_out.model,
                prompt_version=worker_out.prompt_version,
                tokens_in=worker_out.tokens_in,
                tokens_out=worker_out.tokens_out,
                cost_usd=worker_out.cost_usd,
                latency_ms=worker_out.latency_ms or worker_latency,
                retries=worker_out.retries,
                transcript_hash=worker_out.transcript_hash,
                status=TraceStatus.ABSTAINED if worker_out.abstain else TraceStatus.OK,
                detail=worker_out.abstain_reason if worker_out.abstain else None,
            )
            spans.append(worker_span)

            # Worker abstained → LOW_CONFIDENCE
            if worker_out.abstain:
                return RecordRouting(
                    record_id=record.id,
                    action="exception",
                    reason_code=ReasonCode.LOW_CONFIDENCE,
                    reason_class=ReasonClass.A,
                    detail=worker_out.abstain_reason or "Worker abstained",
                    steps_used=steps,
                    cost_usd=total_cost,
                ), spans

            # ── Verifier call ──────────────────────────────────────────────
            if steps >= inp.max_steps_per_record:
                routing, loop_span = self._agent_loop(record, steps, total_cost)
                spans.append(loop_span)
                return routing, spans

            from output_schema import ALLOWED_FIELDS
            verifier_inp = VerifierInput(
                source=record,
                worker_output=worker_out,
                allowed_delivered_fields=list(ALLOWED_FIELDS),
                replay_llm=inp.replay_llm,
            )
            t0 = time.time()
            verifier_out = self.verifier.process(verifier_inp)
            verifier_latency = (time.time() - t0) * 1000
            steps += 1
            total_cost += verifier_out.cost_usd

            verifier_span = AgentSpan(
                agent=self.verifier.name,
                model=verifier_out.transcript_hash and "gpt-4o-mini" or None,
                prompt_version="verifier-v1",
                tokens_in=verifier_out.tokens_in,
                tokens_out=verifier_out.tokens_out,
                cost_usd=verifier_out.cost_usd,
                latency_ms=verifier_out.latency_ms or verifier_latency,
                transcript_hash=verifier_out.transcript_hash,
                status=_verdict_to_status(verifier_out.verdict),
                verdict=verifier_out.verdict.value,
                detail=verifier_out.detail,
            )
            spans.append(verifier_span)

            if verifier_out.verdict == VerifierVerdict.PASS:
                # [OK] Clean path — record is approved for delivery
                self._last_worker_outputs[record.id] = worker_out
                return RecordRouting(
                    record_id=record.id,
                    action="assemble",
                    reason_code=None,
                    reason_class=None,
                    detail="Verifier approved",
                    steps_used=steps,
                    cost_usd=total_cost,
                ), spans

            elif verifier_out.verdict == VerifierVerdict.FAIL:
                if attempt < max_worker_attempts:
                    # Retry with escalated model
                    escalate = True
                    worker_span.status = TraceStatus.RETRIED
                    continue
                # Verifier rejected after retries → exception
                return RecordRouting(
                    record_id=record.id,
                    action="exception",
                    reason_code=verifier_out.reason_code or ReasonCode.AGENT_HALLUCINATION,
                    reason_class=ReasonClass.A,
                    detail=f"Verifier OVERRULED Worker after {attempt+1} attempt(s): {verifier_out.detail}",
                    steps_used=steps,
                    cost_usd=total_cost,
                ), spans

            else:  # needs_human
                return RecordRouting(
                    record_id=record.id,
                    action="exception",
                    reason_code=ReasonCode.LOW_CONFIDENCE,
                    reason_class=ReasonClass.A,
                    detail=f"Verifier requested human review: {verifier_out.detail}",
                    steps_used=steps,
                    cost_usd=total_cost,
                ), spans

        # Should not reach here, but safety net
        return RecordRouting(
            record_id=record.id,
            action="exception",
            reason_code=ReasonCode.AGENT_MALFORMED,
            reason_class=ReasonClass.A,
            detail="Orchestrator exhausted all attempts",
            steps_used=steps,
            cost_usd=total_cost,
        ), spans

    # ── Budget / loop helpers ─────────────────────────────────────────────────

    def _budget_exceeded(
        self, record: NormalizedRecord, steps: int, cost: float
    ) -> tuple[RecordRouting, AgentSpan]:
        span = AgentSpan(
            agent=self.name,
            status=TraceStatus.KILLED,
            detail=f"BUDGET_EXCEEDED: cost ${cost:.5f} >= ${MAX_COST_USD_PER_RECORD}",
        )
        routing = RecordRouting(
            record_id=record.id,
            action="exception",
            reason_code=ReasonCode.BUDGET_EXCEEDED,
            reason_class=ReasonClass.A,
            detail=f"Per-record cost ceiling exceeded: ${cost:.5f}",
            steps_used=steps,
            cost_usd=cost,
        )
        return routing, span

    def _agent_loop(
        self, record: NormalizedRecord, steps: int, cost: float
    ) -> tuple[RecordRouting, AgentSpan]:
        span = AgentSpan(
            agent=self.name,
            status=TraceStatus.KILLED,
            detail=f"AGENT_LOOP: steps {steps} >= {MAX_STEPS_PER_RECORD}",
        )
        routing = RecordRouting(
            record_id=record.id,
            action="exception",
            reason_code=ReasonCode.AGENT_LOOP,
            reason_class=ReasonClass.A,
            detail=f"Step budget exceeded: {steps} steps",
            steps_used=steps,
            cost_usd=cost,
        )
        return routing, span


def _verdict_to_status(verdict: VerifierVerdict) -> TraceStatus:
    return {
        VerifierVerdict.PASS: TraceStatus.OK,
        VerifierVerdict.FAIL: TraceStatus.OVERRULED,
        VerifierVerdict.NEEDS_HUMAN: TraceStatus.ROUTED,
    }[verdict]
