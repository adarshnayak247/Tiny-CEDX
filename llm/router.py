"""
llm/router.py — Model router: cheap vs strong model selection.

Policy (documented in DECISIONS.md):
  - Default (cheap) model: gpt-4o-mini / claude-3-5-haiku / gemini-1.5-flash
    Used for: standard records (ONBOARDING, RENEWAL, REPORT) with normal amounts
  - Strong model: gpt-4o / claude-3-opus / gemini-1.5-pro
    Used for: elevated-risk records, after 1+ retries, or explicit escalation

Rationale: ~80% of records are standard → cheap model handles them.
Strong model only for high-value (>20k) or retry-needed cases.
This keeps cost well below $0.001/record on average.
"""
from __future__ import annotations

import os
from agents.contracts import NormalizedRecord

# ── Environment-configured model names ──────────────────────────────────────
# Default cheap model (used for ~80% of records)
CHEAP_MODEL = os.getenv("LLM_MODEL", "gpt-4o-mini")

# Strong model (escalation). You can override via env.
STRONG_MODEL = os.getenv("LLM_STRONG_MODEL", "gpt-4o")

# Amount threshold above which we escalate to the strong model
HIGH_VALUE_THRESHOLD = float(os.getenv("HIGH_VALUE_THRESHOLD", "20000"))

# Categories that warrant the strong model (higher stakes)
HIGH_STAKES_CATEGORIES = {"REVIEW"}


def select_model(
    record: NormalizedRecord,
    retries: int = 0,
    escalate: bool = False,
) -> str:
    """
    Choose cheap or strong model based on record characteristics.

    Args:
        record:   The normalized record being processed.
        retries:  How many times the Worker has already retried for this record.
        escalate: True if the Verifier rejected the previous attempt.

    Returns:
        Model name string.
    """
    # Always escalate after any failure
    if escalate or retries >= 1:
        return STRONG_MODEL

    # High-value records need stronger reasoning
    if record.amount is not None and record.amount >= HIGH_VALUE_THRESHOLD:
        return STRONG_MODEL

    # High-stakes categories
    if record.category in HIGH_STAKES_CATEGORIES:
        return STRONG_MODEL

    return CHEAP_MODEL


def estimate_cost(model: str, tokens_in: int, tokens_out: int) -> float:
    """
    Rough cost estimate in USD. Used for budget enforcement.
    Prices as of mid-2025 (update in DECISIONS.md if pricing changes).
    """
    pricing = {
        # model: (input_per_1k, output_per_1k)
        "gpt-4o-mini":                    (0.00015, 0.00060),
        "gpt-4o":                         (0.00500, 0.01500),
        "claude-3-5-haiku-20241022":      (0.00025, 0.00125),
        "claude-3-opus-20240229":         (0.01500, 0.07500),
        "gemini-1.5-flash":               (0.00000, 0.00000),   # free tier approx
        "gemini-1.5-pro":                 (0.00350, 0.01050),
    }
    in_rate, out_rate = pricing.get(model, (0.005, 0.015))  # fallback to gpt-4o rates
    return (tokens_in / 1000) * in_rate + (tokens_out / 1000) * out_rate
