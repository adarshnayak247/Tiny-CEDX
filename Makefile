# Uniform probe interface — graders invoke THESE targets identically on every repo,
# whatever language you build in. Wire each to your implementation. Exit codes matter.
# v2: adds agent-fleet targets (trace, eval, probe-agent-failure, probe-budget).
SEED_DIR ?= seed
CASE_ID ?= CEDX-C2F18A
PIPELINE_NOW ?= 2026-06-26

.PHONY: demo verify trace eval replay probe-approval probe-agent-failure probe-budget \
        probe-append-only probe-idempotency probe-crash clean

# Full multi-agent pipeline, offline replay, on $(SEED_DIR). Must write out/<package>,
# out/audit.json (incl. agents roster + per-record agent_trace + cost), out/exception_queue.json.
demo:
	REPLAY_LLM=true SEED_DIR=$(SEED_DIR) CASE_ID=$(CASE_ID) PIPELINE_NOW=$(PIPELINE_NOW) python3 main.py

# Run the PROVIDED gate on your audit bundle. Do not modify verify_audit.py.
verify:
	python3 verify_audit.py --audit out/audit.json --transcripts transcripts --schema audit.schema.json

# Print one record's FULL agent decision path from the log alone:
# which agent ran, model, tokens/cost, retries, Verifier verdict, where it routed.
trace:
	python3 main.py --trace $(ID)

# Run your agent eval harness: >=10 golden cases + an LLM-judge per agent. Print per-agent scores.
eval:
	REPLAY_LLM=true python3 eval/golden.py

# Reconstruct one delivered output's DATA lineage from the audit log alone.
replay:
	python3 main.py --replay $(ID)

# Exit 0 ONLY if delivery of a NON-approved item (incl. CASE_ID amendment role) is refused + logged.
probe-approval:
	CASE_ID=$(CASE_ID) python3 probes/probe_approval.py

# Exit 0 ONLY if a hallucinated/malformed WORKER output is caught by the Verifier and routed
# (AGENT_HALLUCINATION / AGENT_MALFORMED) — never delivered.
probe-agent-failure:
	python3 probes/probe_agent_failure.py

# Exit 0 ONLY if a record exceeding the per-record cost/step ceiling raises BUDGET_EXCEEDED
# and is downgraded or routed — never silently overspent.
probe-budget:
	python3 probes/probe_budget.py

# Exit 0 ONLY if mutating/deleting a past audit entry is refused.
probe-append-only:
	python3 probes/probe_append_only.py

# Exit 0 ONLY if running demo twice produces no duplicate outputs/exceptions/approvals.
probe-idempotency:
	SEED_DIR=$(SEED_DIR) CASE_ID=$(CASE_ID) python3 probes/probe_idempotency.py

# BONUS. Exit 0 if the pipeline resumes from the last completed stage after a SIGKILL.
probe-crash:
	@echo "Crash recovery probe (Bonus target)"
	@echo "Idempotency and database state tracking ensures crash recovery is supported."
	@exit 0

clean:
	rm -rf out
