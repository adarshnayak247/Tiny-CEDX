#!/usr/bin/env python3
"""
probes/probe_idempotency.py — make probe-idempotency

Exit 0 ONLY if running the demo pipeline twice produces no duplicate
outputs, exceptions, or approvals in the second run.

Strategy: Run the pipeline twice (both times REPLAY_LLM=true),
compare the audit.json records list — same IDs, same statuses, no extras.
"""
import json
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from main import run_pipeline

def main() -> int:
    print("[probe-idempotency] Running pipeline twice...")
    errors = []

    # ── Run 1 ────────────────────────────────────────────────────────────────
    print("\n  --- Run 1 ---")
    rc = run_pipeline(run_id="idempotency-run-1")
    if rc != 0:
        errors.append("FAIL: First pipeline run failed")

    audit_path = Path("out/audit.json")
    if not audit_path.exists():
        print("FAIL: out/audit.json not created by first run", file=sys.stderr)
        return 1

    audit1 = json.loads(audit_path.read_text())
    ids1 = {r["id"]: r["status"] for r in audit1.get("records", [])}
    print(f"  Run 1: {len(ids1)} records")

    # ── Run 2 ────────────────────────────────────────────────────────────────
    print("\n  --- Run 2 ---")
    rc2 = run_pipeline(run_id="idempotency-run-2")
    if rc2 != 0:
        errors.append("FAIL: Second pipeline run failed")

    audit2 = json.loads(audit_path.read_text())
    ids2 = {r["id"]: r["status"] for r in audit2.get("records", [])}
    print(f"  Run 2: {len(ids2)} records")

    # ── Compare ──────────────────────────────────────────────────────────────
    # Same record IDs in both runs
    if set(ids1.keys()) != set(ids2.keys()):
        extra1 = set(ids1) - set(ids2)
        extra2 = set(ids2) - set(ids1)
        if extra1:
            errors.append(f"FAIL: Records in run 1 missing from run 2: {extra1}")
        if extra2:
            errors.append(f"FAIL: Extra records appeared in run 2: {extra2}")

    # Same statuses
    for rec_id in ids1:
        if rec_id in ids2 and ids1[rec_id] != ids2[rec_id]:
            errors.append(
                f"FAIL: Record {rec_id} status changed between runs: "
                f"{ids1[rec_id]!r} → {ids2[rec_id]!r}"
            )

    # No extra delivered records in run 2 (idempotency)
    delivered2 = [r for r in audit2.get("records", []) if r.get("status") == "delivered"]
    delivered1 = [r for r in audit1.get("records", []) if r.get("status") == "delivered"]
    if len(delivered2) > len(delivered1):
        errors.append(
            f"FAIL: More delivered records in run 2 ({len(delivered2)}) than run 1 ({len(delivered1)})"
        )
    else:
        print(f"  [OK] Delivered count stable: {len(delivered1)} -> {len(delivered2)}")

    if errors:
        for e in errors:
            print(f"\n{e}", file=sys.stderr)
        return 1

    print(f"\n  [OK] probe-idempotency PASS: runs are idempotent ({len(ids1)} records, same result)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
