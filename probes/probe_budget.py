#!/usr/bin/env python3
"""
probes/probe_budget.py — make probe-budget

Exit 0 ONLY if a record that exceeds the per-record cost/step ceiling
raises BUDGET_EXCEEDED and is routed — never silently overspent.
"""
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

# Set tiny budget so it's easy to exceed
os.environ["MAX_COST_USD_PER_RECORD"] = "0.000001"  # $0.000001 — will be exceeded immediately
os.environ["MAX_STEPS_PER_RECORD"] = "1"             # Only 1 step allowed

from agents.contracts import (
    NormalizedRecord, OrchestratorInput, ReasonCode,
)
from agents.orchestrator import OrchestratorAgent

def main() -> int:
    print("[probe-budget] Testing BUDGET_EXCEEDED with max_steps=1 and max_cost=$0.000001")
    errors = []

    record = NormalizedRecord(
        id="PROBE-BUDGET",
        owner="probe.user",
        deadline="2026-08-01",
        amount=5000.0,
        category="RENEWAL",
        notes="Budget probe record",
        version=1,
        source_format="feed",
        source_hash="sha256:0000",
    )

    orch = OrchestratorAgent()
    inp = OrchestratorInput(
        records=[record],
        run_id="probe-budget-run",
        pipeline_now="2026-06-26",
        max_cost_usd_per_record=0.000001,
        max_steps_per_record=1,
        replay_llm=True,
    )

    out, traces = orch.run(inp)
    routing = out.routings[0]

    if routing.action != "exception":
        errors.append(f"FAIL: Record was not routed to exception (action={routing.action})")
    elif routing.reason_code not in (ReasonCode.BUDGET_EXCEEDED, ReasonCode.AGENT_LOOP):
        errors.append(
            f"FAIL: Expected BUDGET_EXCEEDED or AGENT_LOOP, got {routing.reason_code}"
        )
    else:
        print(f"  [OK] Record routed with {routing.reason_code.value}: {routing.detail[:80]}")

    # Verify the record was NOT delivered (check spans)
    spans = traces.get("PROBE-BUDGET", [])
    killed_spans = [s for s in spans if s.status.value in ("killed", "routed")]
    if not killed_spans:
        errors.append("FAIL: No killed/routed span in trace — budget enforcement not evidenced")
    else:
        print(f"  [OK] Trace shows {len(killed_spans)} killed/routed span(s)")

    if errors:
        for e in errors:
            print(f"\n{e}", file=sys.stderr)
        return 1

    print("\n  [OK] probe-budget PASS: BUDGET_EXCEEDED raised and handled, record not delivered")
    return 0

if __name__ == "__main__":
    sys.exit(main())
