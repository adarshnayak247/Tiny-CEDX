"""
pipeline/assembly.py — Stage 3: Calls the Orchestrator which calls Worker + Verifier.

This stage:
  - Takes clean normalized records from Orchestration
  - Passes them to the Orchestrator agent (which manages Worker + Verifier)
  - Collects per-record traces, routing decisions, and assembled outputs
  - Returns assembled records (those approved by Verifier) + new exceptions (agent-layer)
"""
from __future__ import annotations

import os
from typing import Any

from agents.contracts import (
    AgentSpan, ExceptionRecord, NormalizedRecord, OrchestratorInput,
    ReasonClass, WorkerOutput, sha256_hex,
)
from agents.orchestrator import OrchestratorAgent

REPLAY_LLM = os.getenv("REPLAY_LLM", "true").lower() != "false"


class AssemblyStage:
    """
    Coordinates the Orchestrator → Worker → Verifier chain.
    Returns assembled outputs and any agent-layer exceptions.
    """

    def __init__(self):
        self.orchestrator = OrchestratorAgent()

    def run(
        self,
        clean_records: list[NormalizedRecord],
        run_id: str,
    ) -> tuple[
        dict[str, WorkerOutput],        # record_id → approved WorkerOutput
        list[ExceptionRecord],           # agent-layer exceptions
        dict[str, list[AgentSpan]],      # record_id → trace spans
    ]:
        if not clean_records:
            return {}, [], {}

        inp = OrchestratorInput(
            records=clean_records,
            run_id=run_id,
            pipeline_now=os.getenv("PIPELINE_NOW", "2026-06-26"),
            replay_llm=REPLAY_LLM,
        )

        orch_out, traces = self.orchestrator.run(inp)

        # Build lookup: record_id → NormalizedRecord
        record_map = {r.id: r for r in clean_records}

        approved: dict[str, WorkerOutput] = {}
        agent_exceptions: list[ExceptionRecord] = []

        for routing in orch_out.routings:
            if routing.action == "assemble":
                # Extract the last Worker output from the trace
                worker_out = self._extract_worker_output(
                    routing.record_id, traces.get(routing.record_id, [])
                )
                if worker_out:
                    approved[routing.record_id] = worker_out
            elif routing.action == "exception" and routing.reason_code:
                rec = record_map.get(routing.record_id)
                agent_exceptions.append(ExceptionRecord(
                    id=routing.record_id,
                    source_format=rec.source_format if rec else "unknown",
                    reason_code=routing.reason_code,
                    reason_class=routing.reason_class or ReasonClass.A,
                    detail=routing.detail or "",
                    version=rec.version if rec else 1,
                    source_hash=rec.source_hash if rec else "",
                ))

        return approved, agent_exceptions, traces

    def _extract_worker_output(
        self, record_id: str, spans: list[AgentSpan]
    ) -> WorkerOutput | None:
        """
        Reconstruct the WorkerOutput from the trace spans.
        The actual WorkerOutput is stored on the span via the orchestrator.
        This is a simplified reconstruction — in production you'd pass the
        object directly, but we store it in the orchestrator during the run.
        """
        # The orchestrator stores worker outputs during its run.
        # Access them via the orchestrator's internal cache.
        return self.orchestrator._last_worker_outputs.get(record_id)
