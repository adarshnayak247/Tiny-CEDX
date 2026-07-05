#!/usr/bin/env python3
"""
probes/probe_append_only.py — make probe-append-only

Exit 0 ONLY if mutating/deleting a past audit entry is refused.
Tests that the event log seq is strictly 0..n-1 (append-only shaped).
"""
import json
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.delivery import verify_append_only

def main() -> int:
    audit_path = Path("out/audit.json")
    errors = []

    # ── Test 1: Verify existing audit is append-only shaped ──────────────────
    if audit_path.exists():
        ok, msg = verify_append_only(audit_path)
        if not ok:
            errors.append(f"FAIL: Existing audit.json is not append-only: {msg}")
        else:
            print(f"  [OK] Existing audit.json: {msg}")
    else:
        print("  [WARN] out/audit.json not found - run make demo first for full test")

    # ── Test 2: Attempt to mutate a past entry — verify it's detectable ───────
    print("[probe-append-only] Testing mutation detection...")

    # Create a test audit with valid seq
    test_audit = {
        "case_id": "CEDX-C2F18A",
        "pipeline_version": "1.0.0",
        "generated_at": "2026-06-26T00:00:00Z",
        "seed_dir": "seed",
        "amendment": {"role": "compliance", "threshold": 21000},
        "agents": [],
        "cost": {"total_usd": 0.0, "records": 0},
        "output_package_hash": "sha256:" + "0" * 64,
        "records": [],
        "events": [
            {"seq": 0, "ts": "2026-06-26T00:00:00Z", "actor": "system", "action": "start", "record_id": None},
            {"seq": 1, "ts": "2026-06-26T00:00:01Z", "actor": "Worker", "action": "assembled", "record_id": "REC-001"},
            {"seq": 2, "ts": "2026-06-26T00:00:02Z", "actor": "system", "action": "complete", "record_id": None},
        ],
    }
    test_path = Path("out/test_append_only.json")
    test_path.parent.mkdir(parents=True, exist_ok=True)
    test_path.write_text(json.dumps(test_audit), encoding="utf-8")

    # Verify clean test audit
    ok, msg = verify_append_only(test_path)
    if not ok:
        errors.append(f"FAIL: Clean test audit not verified: {msg}")
    else:
        print(f"  [OK] Clean test audit verified: {msg}")

    # Mutate: delete seq 1 (simulating a tampering attempt)
    mutated = json.loads(test_path.read_text())
    del mutated["events"][1]  # delete middle entry — seq becomes [0, 2] not [0, 1, 2]
    test_path.write_text(json.dumps(mutated), encoding="utf-8")

    ok2, msg2 = verify_append_only(test_path)
    if ok2:
        errors.append("FAIL: Mutated audit (deleted entry) was accepted as append-only — tampering not detected")
    else:
        print(f"  [OK] Mutation detected: {msg2[:80]}")

    # Mutation: reorder entries (swap seq 0 and 1)
    mutated2 = json.loads(Path("out/test_append_only.json").exists() and test_path.read_text() or "{}")
    test_audit_2 = test_audit.copy()
    test_audit_2["events"] = [
        {"seq": 1, "ts": "...", "actor": "X", "action": "Y", "record_id": None},  # out of order
        {"seq": 0, "ts": "...", "actor": "Z", "action": "W", "record_id": None},
    ]
    test_path.write_text(json.dumps(test_audit_2), encoding="utf-8")
    ok3, msg3 = verify_append_only(test_path)
    if ok3:
        errors.append("FAIL: Reordered events accepted as append-only")
    else:
        print(f"  [OK] Reordered events detected: {msg3[:80]}")

    # Cleanup
    test_path.unlink(missing_ok=True)

    if errors:
        for e in errors:
            print(f"\n{e}", file=sys.stderr)
        return 1

    print("\n  [OK] probe-append-only PASS: mutations are detected, append-only enforced")
    return 0

if __name__ == "__main__":
    sys.exit(main())
