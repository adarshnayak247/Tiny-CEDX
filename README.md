# CEDX Multi-Agent Legal Fleet
**Automated Case Management & Litigation Pipeline**

This repository contains my solution for the CEDX AI Systems task. It implements a fully functional, production-ready AI agent fleet tailored for **Legal Services**, specializing in processing legal intake requests, tracking litigation statuses, and summarizing discovery materials.

The architecture emphasizes reliability, explicit typed boundaries, and independent agent-checks-agent verification to ensure AI outputs are strictly governed before reaching production.

---

## 1. Industry & Scope
- **Industry Selection:** Legal Services (Case Management & Litigation Support)
- **Scope Details:** Ingesting multi-format case files (JSON, PDF, email), parsing fields, evaluating risk priority, executing LLM-based case merit analyses, isolating anomalies, and packaging approved legal briefs.
- **Pipeline Tier:** Tier 1 Standard (3 explicitly scoped agents, 5-stage pipeline, fully evaluable, resilient offline playback)
- **CASE_ID:** `CEDX-C2F18A`

---

## 2. Agent Topology
Instead of a monolithic script, this system is organized into a true fleet of explicitly contracted agents (found in `agents/`):

- **Orchestrator (`agents/orchestrator.py`):** The pipeline manager. It governs budget and routing logic without directly evaluating text. It decides if a record proceeds to Assembly or is sent to the exception queue (enforcing a strict step budget and $0.05 limit).
- **Worker (`agents/worker.py`):** The creative engine. Leverages LLMs to digest normalized legal records and draft standardized, branded case briefs. It dynamically routes simple cases to economical models and complex cases to advanced models.
- **Verifier (`agents/verifier.py`):** The independent critic. It explicitly checks the Worker's draft against the raw source document. It holds "overrule authority" to intercept hallucinations or schema violations (`AGENT_HALLUCINATION` / `AGENT_MALFORMED`).

*Typed contracts mapping inputs, outputs, and permissions are defined in `agents/contracts.py`.*

---

## 3. How to Run

### Offline Deterministic Replay (Default)
To run the full end-to-end multi-agent pipeline offline using committed transcripts (zero cost, immediate execution):
```bash
make demo
```
*(Alternatively: `docker compose up`)*

### Run Automated Audits
To mathematically prove the output conforms strictly to the schema constraints:
```bash
make verify
```

### Traceability
To trace the complete lineage of an individual record (e.g. `REC-001`):
```bash
make trace ID=REC-001
# Or to replay the data transformations:
make replay ID=REC-001
```

### Live LLM Execution
To execute the pipeline against real LLM APIs (OpenAI/Anthropic/Gemini):
```bash
export REPLAY_LLM=false
export LLM_API_KEY="your-api-key"
make demo
```

---

## 4. Controls
A robust suite of diagnostic probes is provided to grade the system's defenses:

- `make probe-approval`: Ensures the state-machine blocks unapproved items, specifically validating the CASE_ID amendment logic.
- `make probe-agent-failure`: Confirms the Verifier detects maliciously injected worker hallucinations and routes them to exceptions.
- `make probe-budget`: Demonstrates the Orchestrator gracefully handling logic loops or API costs exceeding the ceiling.
- `make probe-append-only`: Validates cryptographic sequencing of the `audit.json` log.
- `make probe-idempotency`: Asserts running the pipeline multiple times does not duplicate state or side-effects.
- `make eval`: Initiates the LLM-judge framework against 10 golden scenarios.

---

## 5. Planted-Problem Handling (Exceptions)

The system automatically detects anomalies at both the data and agent layers:

| Layer | Issue Detected | Exception Reason Code | Resolution Logic |
|---|---|---|---|
| **Data** | Missed litigation deadline | `STALE` | Checked against `PIPELINE_NOW` env variable |
| **Data** | Missing essential field | `MISSING_INPUT` | Caught during schema normalization step |
| **Data** | Anomalous claim amounts | `OUTLIER` | Evaluated against dynamic interquartile ranges (`10 * IQR`) |
| **Data** | Subversion attempt | `INJECTION_BLOCKED` | Regex parsing blocks prompt override attempts |
| **Data** | Unclear legal facts | `LOW_CONFIDENCE` | Trapped by ambiguity heuristics in parsing |
| **Data** | Updated motion filed | `SUPERSEDED_VERSION` | Previous entries are marked inactive and superseded |
| **Agent** | Invented case facts | `AGENT_HALLUCINATION` | Verifier strictly diffs worker drafts against source data |
| **Agent** | Malformed JSON schema | `AGENT_MALFORMED` | Verifier catches Pydantic validation failures |
| **Agent** | Infinite processing loop | `AGENT_LOOP` | Orchestrator kills execution at the step ceiling limit |
| **Agent** | High API token usage | `BUDGET_EXCEEDED` | Orchestrator halts record processing at $0.05 limit |

---

## 6. Generalization
All security and filtering logic is designed to generalize. For instance, `OUTLIER` boundaries are computed dynamically based on the statistical median and IQR of the current batch of cases rather than hard-coded constants. The prompt injection safeguards rely on regex pattern matching of structural bypass terminology rather than exact string matches.

---

## 7. LLM/Agent Contract & Eval
Every agent handoff is strictly validated using Pydantic schemas. 
The system utilizes a 10-case Golden Evaluation harness (`eval/golden.py`) to mathematically grade accuracy. 
**Current Harness Results:**
- **Orchestrator Conformance:** 100.0%
- **Worker Accuracy:** 100.0%
- **Verifier Reliability:** 100.0%

---

## 8. Cost & Scale
By actively monitoring token usage and implementing a Model Router (where simple status reports default to `gpt-4o-mini` and complex litigation escalates to `gpt-4o`), costs are heavily optimized.
- **Average Cost per Record:** ~$0.0009
- **p95 Execution Latency (Replay):** ~0.2ms
- **Projected 10k Scale Economics:** ~$9.00 - $10.00 per day.

---

## 9. Amendment
The dynamic Maker-Checker gate has been implemented using my specific `CASE_ID`.
For `CEDX-C2F18A`, the hash generated requirements mandate:
- **Secondary Role:** `finance_controller`
- **Threshold Limit:** `$25,000`
Any case processed with an amount >= 25,000 is blocked until an explicit system approval is logged by a finance_controller, ensuring high-risk settlements aren't delivered autonomously.

---

## 10. AI Usage / Real-vs-Faked
This pipeline relies on real operational logic. `REPLAY_LLM=true` utilizes real pre-recorded JSON transcripts, meaning the system mathematically simulates API latency, token tracking, and hash validation, while executing real python state machine logic for routing and auditing. There are no faked delivery arrays—the code processes data exactly as it would in production.

---

## 11. Tradeoffs & Next Week
- **Current Tradeoff:** To prevent heavy third-party dependencies (`pypdf`), the application relies on a custom stream decoder for vector PDFs. While incredibly fast, it will fail on scanned, raster-based court filings.
- **Next Week:** 
  1. Implement an async task queue (like Celery) for horizontal scaling of the Worker agent.
  2. Implement an OCR fallback for image-based PDFs using a vision model.
  3. Upgrade the local SQLite state-store to a managed PostgreSQL cluster for distributed state lock handling.
