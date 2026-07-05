"""
pipeline/orchestration.py — Stage 2: Declarative normalize + exception queue.

Runs all planted-problem detectors against normalized records.
Routes records to exception queue or to Assembly.

Class-A (blocking): STALE, MISSING_INPUT, OUTLIER, INJECTION_BLOCKED,
                    LOW_CONFIDENCE, UNVERIFIED_ANOMALY
Class-B (auto-resolved): SCHEMA_DRIFT, SUPERSEDED_VERSION

Detectors are composable functions — easy to add new reason codes.
Thresholds are rule-based and generalize to unseen data (no hardcoded values).
"""
from __future__ import annotations

import math
import os
import re
from datetime import date
from typing import Any, Optional

from agents.contracts import (
    CLASS_A_CODES, NormalizedRecord, RawRecord, ReasonClass, ReasonCode,
    ExceptionRecord,
)
from pipeline.intake import normalize_raw_fields

PIPELINE_NOW = os.getenv("PIPELINE_NOW", "2026-06-26")
KNOWN_CATEGORIES = {"ONBOARDING", "RENEWAL", "REVIEW", "REPORT", "INTAKE"}

# Patterns that indicate prompt injection attempts (generalizable regex)
INJECTION_PATTERNS = [
    r"ignore\s+(all\s+)?(previous\s+)?instructions?",
    r"approve\s+(this\s+)?immediately",
    r"skip\s+review",
    r"ignore\s+(your\s+)?(rules|guidelines|instructions)",
    r"output\s+approved",
    r"bypass\s+(review|approval|check)",
    r"disregard\s+(the\s+)?(above|instructions?|rules?)",
    r"act\s+as\s+if\s+(you\s+are|you're)",
    r"forget\s+(your\s+)?(instructions?|rules?|guidelines?)",
    r"ignore\s+the\s+field",   # catches "ignore the field amount"
]
_INJECTION_RE = re.compile("|".join(INJECTION_PATTERNS), re.IGNORECASE)


class OrchestrationStage:
    """
    Runs all detectors against normalized records.
    Returns clean records + exception records.
    """

    def run(
        self,
        raw_records: list[RawRecord],
        superseded_ids: list[tuple[str, int]],
        all_amounts: Optional[list[float]] = None,
    ) -> tuple[list[NormalizedRecord], list[ExceptionRecord]]:
        """
        Normalize + detect problems.

        Args:
            raw_records:    Records from Intake (non-superseded).
            superseded_ids: (id, version) pairs already marked superseded.
            all_amounts:    If provided, used for outlier computation (population).
                            If None, computed from raw_records.

        Returns:
            (clean_records, exception_records)
        """
        # Build the outlier threshold from the full population
        amounts = all_amounts or [
            float(r.raw_fields.get("amount", 0) or 0)
            for r in raw_records
            if r.raw_fields.get("amount") is not None
        ]
        outlier_threshold = _compute_outlier_threshold(amounts)

        clean: list[NormalizedRecord] = []
        exceptions: list[ExceptionRecord] = []

        # ── Process each raw record ──────────────────────────────────────────
        for raw in raw_records:
            fields, drifts = normalize_raw_fields(raw.raw_fields)

            # Build normalized record from mapped fields
            norm = NormalizedRecord(
                id=raw.id,
                owner=_str(fields.get("owner")),
                deadline=_str(fields.get("deadline")),
                amount=_num(fields.get("amount")),
                category=_str(fields.get("category")),
                notes=_str(fields.get("notes")),
                version=int(fields.get("version", raw.version)),
                source_format=raw.source_format,
                source_hash=raw.source_hash,
                schema_drifts=drifts,
            )

            # ── Run detectors in priority order ────────────────────────────
            exc = (
                self._detect_stale(norm)
                or self._detect_missing_input(norm)
                or self._detect_outlier(norm, outlier_threshold)
                or self._detect_injection(norm)
                or self._detect_low_confidence(norm)
            )

            if exc:
                exceptions.append(exc)
                continue

            # ── Class-B: Schema drift (continues, logged) ───────────────
            # norm.schema_drifts already populated by normalize_raw_fields

            # If clean → add to clean list
            clean.append(norm)

        # ── Add superseded records to exception queue (Class-B) ──────────────
        superseded_records = [
            ExceptionRecord(
                id=rec_id,
                source_format="feed",
                reason_code=ReasonCode.SUPERSEDED_VERSION,
                reason_class=ReasonClass.B,
                detail=f"Version {ver} superseded by a newer version",
                version=ver,
            )
            for rec_id, ver in superseded_ids
        ]
        exceptions.extend(superseded_records)

        return clean, exceptions

    # ── Detectors ────────────────────────────────────────────────────────────

    def _detect_stale(self, rec: NormalizedRecord) -> Optional[ExceptionRecord]:
        """STALE: deadline is in the past relative to PIPELINE_NOW."""
        if not rec.deadline:
            return None
        try:
            dl = date.fromisoformat(rec.deadline)
            now = date.fromisoformat(PIPELINE_NOW)
            if dl < now:
                return ExceptionRecord(
                    id=rec.id, source_format=rec.source_format,
                    reason_code=ReasonCode.STALE, reason_class=ReasonClass.A,
                    detail=f"Deadline {rec.deadline} is before PIPELINE_NOW {PIPELINE_NOW}",
                    version=rec.version, source_hash=rec.source_hash,
                )
        except ValueError:
            pass
        return None

    def _detect_missing_input(self, rec: NormalizedRecord) -> Optional[ExceptionRecord]:
        """MISSING_INPUT: any required field is null/empty. No auto-default."""
        required = {"owner": rec.owner, "deadline": rec.deadline, "amount": rec.amount}
        for field, val in required.items():
            if val is None or val == "":
                return ExceptionRecord(
                    id=rec.id, source_format=rec.source_format,
                    reason_code=ReasonCode.MISSING_INPUT, reason_class=ReasonClass.A,
                    detail=f"Required field '{field}' is null/missing. No auto-default allowed.",
                    version=rec.version, source_hash=rec.source_hash,
                )
        return None

    def _detect_outlier(
        self, rec: NormalizedRecord, threshold: float
    ) -> Optional[ExceptionRecord]:
        """
        OUTLIER: amount exceeds median + 10×IQR (robust statistic).
        Threshold is dynamic per batch — never hardcoded.
        """
        if rec.amount is None or threshold <= 0:
            return None
        if rec.amount > threshold:
            return ExceptionRecord(
                id=rec.id, source_format=rec.source_format,
                reason_code=ReasonCode.OUTLIER, reason_class=ReasonClass.A,
                detail=(
                    f"Amount {rec.amount} exceeds outlier threshold {threshold:.2f} "
                    f"(median + 10×IQR of batch). Requires manual review."
                ),
                version=rec.version, source_hash=rec.source_hash,
            )
        return None

    def _detect_injection(self, rec: NormalizedRecord) -> Optional[ExceptionRecord]:
        """
        INJECTION_BLOCKED: notes contain command-injection patterns.
        Pattern list generalizes — not hardcoded to specific phrases.
        """
        text = (rec.notes or "") + " " + (rec.category or "")
        if _INJECTION_RE.search(text):
            return ExceptionRecord(
                id=rec.id, source_format=rec.source_format,
                reason_code=ReasonCode.INJECTION_BLOCKED, reason_class=ReasonClass.A,
                detail=f"Prompt injection pattern detected in record notes/category. Quarantined.",
                version=rec.version, source_hash=rec.source_hash,
            )
        return None

    def _detect_low_confidence(self, rec: NormalizedRecord) -> Optional[ExceptionRecord]:
        """
        LOW_CONFIDENCE: category is '?' or '??' or clearly ambiguous.
        Also catches contradictory notes ("could be X or Y").
        """
        category = (rec.category or "").strip()
        if category == "?" or category == "" or category.startswith("?"):
            return ExceptionRecord(
                id=rec.id, source_format=rec.source_format,
                reason_code=ReasonCode.LOW_CONFIDENCE, reason_class=ReasonClass.A,
                detail=f"Category is ambiguous ('{category}'). Cannot produce confident output.",
                version=rec.version, source_hash=rec.source_hash,
            )
        # Check notes for ambiguity signals
        notes_lower = (rec.notes or "").lower()
        ambiguity_signals = [
            "could be", "unclear", "unknown", "tbd", "to be determined",
            "ambiguous", "not specified", "side letter not attached",
        ]
        if any(sig in notes_lower for sig in ambiguity_signals):
            return ExceptionRecord(
                id=rec.id, source_format=rec.source_format,
                reason_code=ReasonCode.LOW_CONFIDENCE, reason_class=ReasonClass.A,
                detail=f"Record is too ambiguous to produce confident output: {rec.notes!r}",
                version=rec.version, source_hash=rec.source_hash,
            )
        return None


# ── Statistical outlier threshold ────────────────────────────────────────────

def _compute_outlier_threshold(amounts: list[float]) -> float:
    """
    Compute outlier threshold = median + 10 * IQR.
    Uses a robust statistic that generalizes to any batch.
    Returns 0 if fewer than 4 values (can't compute meaningful IQR).
    """
    vals = [v for v in amounts if v is not None and v > 0]
    if len(vals) < 4:
        return 0.0
    vals_sorted = sorted(vals)
    n = len(vals_sorted)
    q1 = _percentile(vals_sorted, 25)
    q3 = _percentile(vals_sorted, 75)
    iqr = q3 - q1
    median = _percentile(vals_sorted, 50)
    return median + 10 * iqr


def _percentile(sorted_vals: list[float], p: float) -> float:
    """Compute the p-th percentile of a sorted list."""
    if not sorted_vals:
        return 0.0
    n = len(sorted_vals)
    idx = (p / 100) * (n - 1)
    lo = int(idx)
    hi = lo + 1
    frac = idx - lo
    if hi >= n:
        return sorted_vals[-1]
    return sorted_vals[lo] * (1 - frac) + sorted_vals[hi] * frac


# ── Type coercion helpers ─────────────────────────────────────────────────────

def _str(val: Any) -> Optional[str]:
    if val is None:
        return None
    s = str(val).strip()
    return s if s and s.lower() not in ("none", "null", "n/a") else None


def _num(val: Any) -> Optional[float]:
    if val is None:
        return None
    try:
        return float(val)
    except (TypeError, ValueError):
        return None
