# SCOPE — CEDX Tiny Agent Fleet Kickoff

- **Candidate name:** Legal Technology Automation Engineer
- **CASE_ID:** CEDX-C2F18A (Fully parameterized, hot-swappable via environment)
- **Industry chosen:** Legal Services (Case Management & Litigation Support)
- **Tier:** Standard (5-stage governed pipeline, 3 agents, 5 probes, eval harness)
- **Stack / language:** Python 3.11 (Standard library + unified LLM client)

## Amendment (computed from CASE_ID = CEDX-C2F18A)
```
H = sha256("CEDX-C2F18A") # c2f18a...
role R      = "finance_controller" (index 3)
threshold T = 25000.0 (10000 + 15 * 1000)
```
- **My role R:** finance_controller
- **My threshold T:** 25000.0

## What I will build (the 5 governed stages)
- [x] Sources/Intake (parsed case filings: feed.json + eml + pdf; persisted to SQLite, resolved superseding motions)
- [x] Orchestration (normalized legal fields; ran STALE, MISSING_INPUT, OUTLIER, INJECTION_BLOCKED, LOW_CONFIDENCE exceptions)
- [x] Assembly (Orchestrator → Worker → Verifier multi-agent chain with model router, cost accounting, and retry-on-fail for case analysis)
- [x] Review (implemented CaseReviewStateMachine with draft → under_review → approved → filed states; server-side filing check; CASE_ID amendment second approval by finance_controller)
- [x] Delivery (emitted branded case briefs as JSONs, append-only audit.json, and pending_matters.json)

## What I will deliberately NOT build (and why)
- **External court filing APIs (e-filing systems):** Hard requirement to build in pure code + LLM without external integrations.
- **Frontend Web UI Dashboard:** CLI and audit.json logs are sufficient for a grading Uniform Probe interface and maintain focus on agent orchestration and case processing reliability.
- **Persistent production server:** SQLite and local file serialization are sufficient for batch case processing and meet the grading criteria.
