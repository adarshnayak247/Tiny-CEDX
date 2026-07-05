# DECISIONS — Tiny CEDX Agent Fleet (Legal Services)

This document outlines key technical decisions, generalizations, operational constraints, and edge-case mitigations implemented for the legal case management pipeline.

---

## 1. What We Did NOT Automate & Why

*   **Pending Matters Resolution:** We do not automatically retry Class-A exceptions (e.g. `STALE` or `OUTLIER`) using an LLM. Since these cases violate core legal processing rules, resolving them requires attorney review (e.g. updating the source filing or requesting deadline extensions). Automatically trying to "fix" a stale filing deadline by guessing a new date is dangerous and violates legal compliance.
*   **Arbitrary Field Mapping Schema Changes:** The `field_map.yaml` file lists canonical name maps. If a field name arrives that is completely unknown (not in the map and doesn't match canonical fields), we do not attempt to map it dynamically via an LLM. Dynamic mapping can lead to field collisions or incorrect legal data correlation. Attorney review is required to update the mapping configuration.

---

## 2. Outlier and Abstain Thresholds (Generalization)

*   **Robust Outlier Threshold (Median + 10 × IQR):** Rather than hardcoding a fixed claim limit (e.g., `if claim_amount > 100000`), we use a robust statistical outlier detector.
    *   **IQR (Interquartile Range)** measures the statistical spread of the middle 50% of the case dataset.
    *   **Median** represents the midpoint.
    *   Using `Median + 10 * IQR` protects against single large claim values skewing the mean, ensuring that only true anomalies (like `500000` relative to a ~15,000 average) are flagged as `OUTLIER` cases. This generalizes to *any* batch, regardless of the jurisdiction or claim scale.
*   **Abstain Policy:** The Worker agent is prompted to return `{"abstain": true, "reason": "..."}` if case facts are contradictory, or if critical legal details are missing. Rather than fabricating analysis, the agent gracefully yields, routing the case to the `LOW_CONFIDENCE` exception bucket.

---

## 3. Router Policy & Scale Economics

We implemented a two-tier routing system:
*   **Routine/Standard Cases:** Dispatched to `gpt-4o-mini` (Input: $0.15/1M, Output: $0.60/1M).
*   **High-Value / Complex / Retry Cases:** Dispatched to `gpt-4o` (Input: $5.00/1M, Output: $15.00/1M).

### Scale Economics Projection (at 10,000 cases/day)

Assuming 85% of cases are routine (handled by `gpt-4o-mini`) and 15% are escalated/complex/retry (handled by `gpt-4o`):

| Model | Case Share | Avg. Tokens In | Avg. Tokens Out | Cost / Case | Total Daily Cost (10k) |
|---|---|---|---|---|---|
| `gpt-4o-mini` | 85% | 500 | 150 | $0.000165 | $1.40 |
| `gpt-4o` | 15% | 600 | 200 | $0.006000 | $9.00 |
| **Combined** | **100%** | - | - | **$0.001040** | **$10.40** |

At 10,000 cases/day, the fleet runs for approximately **$10.40/day**, ensuring enterprise-scale viability for law firm operations.

---

## 4. Provenance and Idempotency

*   **SQLite Database Store:** To survive crashes and guarantee idempotency, we store all raw case intake inputs and status flags in a local database `out/intake.db`.
*   **Source Version Hashing:** Every parsed case stores a `source_hash` (SHA-256 of the raw file content or JSON bytes). If the same case is processed twice, its hash is diffed:
    *   If the hash and version match, the database skips re-processing (idempotency).
    *   If the version is higher, it supersedes the previous run, generating a `SUPERSEDED_VERSION` trace event.
*   **Atomic Write & Rename:** Writing files under `out/package/` and writing `out/audit.json` uses an atomic temp-write-then-replace pattern, preventing incomplete writes or state corruption during SIGKILL.

---

## 5. What Breaks First at 10,000 Cases/Day?

1.  **SQLite Lock Contention:** SQLite is a single-file database. While excellent for batch processing and local testing, high concurrency (multiple simultaneous intake processes writing to `intake.db`) will encounter table locks. At 10k cases/day with parallel processing, it must be migrated to a client-server database like PostgreSQL.
2.  **API Rate Limiting:** Making 10,000 real-time LLM requests per day (especially concurrent ones) will hit OpenAI/Anthropic RPM (Requests Per Minute) or TPM (Tokens Per Minute) ceilings. We would need to implement a robust background task queue (e.g. Celery / Redis) with rate-limit bucket throttling.
3.  **PDF/EML Parsing Failures:** Parsing raw PDFs assumes a consistent ReportLab stream structure. If attorneys upload scanned court documents (non-vector), the fallback stream extractor will find zero text. At scale, an OCR pipeline (like Tesseract or AWS Textract) is required.

---

## 6. CASE_ID
*   **Assigned ID:** `CEDX-C2F18A`
*   **Amendment Rule:** Role: `finance_controller`, Threshold: `25000.0`.
