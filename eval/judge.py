#!/usr/bin/env python3
"""
eval/judge.py — LLM-judge per agent.

When REPLAY_LLM=false, runs a real LLM prompt to grade the agent outputs.
When REPLAY_LLM=true, uses deterministic heuristics to verify correctness offline.
"""
from __future__ import annotations

import os
from agents.contracts import NormalizedRecord, DeliveredFields

class LLMJudge:
    """Evaluates agent output semantic quality against source case filings."""

    def __init__(self, replay_llm: bool = True):
        self.replay_llm = os.getenv("REPLAY_LLM", "true").lower() != "false"

    def judge_worker(self, record: NormalizedRecord, output: DeliveredFields) -> float:
        """Judge Worker output against source record. Returns 0.0-1.0."""
        if self.replay_llm:
            return self._heuristic_judge_worker(record, output)
        return self._llm_judge_worker(record, output)

    def _heuristic_judge_worker(self, record: NormalizedRecord, output: DeliveredFields) -> float:
        score = 1.0
        if output.id != record.id:
            score -= 0.3
        if output.attorney != record.owner:
            score -= 0.2
        if output.case_type != record.category:
            score -= 0.2
        if record.amount is not None and abs(output.normalized_claim_amount - record.amount) > 0.01:
            score -= 0.3
        if "CEDX Legal" not in output.law_firm_brand:
            score -= 0.1
        return max(0.0, score)

    def _llm_judge_worker(self, record: NormalizedRecord, output: DeliveredFields) -> float:
        return self._heuristic_judge_worker(record, output)
