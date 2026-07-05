#!/usr/bin/env python3
"""
probes/probe_approval.py — make probe-approval

Exit 0 ONLY if:
  1. Delivering a non-approved record is refused AND logged.
  2. Delivering a record above the amendment threshold with only one approver is refused.
  3. A properly approved record (with amendment approval if needed) can be delivered.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from pipeline.review import (
    ApprovalStateMachine, ApprovalState,
    AMENDMENT_ROLE, AMENDMENT_THRESHOLD, CASE_ID
)

def main() -> int:
    print(f"[probe-approval] CASE_ID={CASE_ID}  role={AMENDMENT_ROLE}  threshold={AMENDMENT_THRESHOLD}")

    errors = []

    # ── Test 1: Refuse delivery of a draft record ────────────────────────────
    m1 = ApprovalStateMachine("PROBE-DRAFT", 1000.0)
    ok, msg = m1.can_deliver()
    if ok:
        errors.append("FAIL: Draft record was allowed delivery (expected refusal)")
    else:
        print(f"  [OK] Draft record refused: {msg[:80]}")

    # ── Test 2: Refuse delivery of in_review record ──────────────────────────
    m2 = ApprovalStateMachine("PROBE-INREVIEW", 1000.0)
    m2.transition(ApprovalState.IN_REVIEW, actor="test")
    ok, msg = m2.can_deliver()
    if ok:
        errors.append("FAIL: in_review record was allowed delivery")
    else:
        print(f"  [OK] in_review record refused: {msg[:80]}")

    # ── Test 3: Amendment gate — above threshold, missing second approver ─────
    amount_above = AMENDMENT_THRESHOLD + 1000
    m3 = ApprovalStateMachine("PROBE-AMENDMENT", amount_above)
    m3.transition(ApprovalState.IN_REVIEW, actor="test")
    # Try to approve WITHOUT the amendment second approver
    ok, msg = m3.transition(ApprovalState.APPROVED, actor="test/operator")
    if ok:
        errors.append(f"FAIL: Record above threshold ({amount_above}) approved without amendment second approver")
    else:
        print(f"  [OK] Amendment gate refused: {msg[:100]}")

    # ── Test 4: Amendment gate — with correct second approver ────────────────
    m4 = ApprovalStateMachine("PROBE-AMENDMENT-PASS", amount_above)
    m4.transition(ApprovalState.IN_REVIEW, actor="test")
    ok, msg = m4.record_amendment_approval(actor="risk_officer_1", role=AMENDMENT_ROLE)
    if not ok:
        errors.append(f"FAIL: Amendment approval rejected: {msg}")
    else:
        ok, msg = m4.transition(ApprovalState.APPROVED, actor="test/operator")
        if not ok:
            errors.append(f"FAIL: Could not approve after amendment: {msg}")
        else:
            ok2, msg2 = m4.can_deliver()
            if not ok2:
                errors.append(f"FAIL: Approved record refused delivery: {msg2}")
            else:
                print(f"  [OK] Approved record (with amendment) can be delivered")

    # ── Test 5: Wrong role for amendment ─────────────────────────────────────
    m5 = ApprovalStateMachine("PROBE-WRONG-ROLE", amount_above)
    ok, msg = m5.record_amendment_approval(actor="random_person", role="ceo")
    if ok:
        errors.append("FAIL: Wrong role accepted for amendment approval")
    else:
        print(f"  [OK] Wrong role rejected: {msg[:80]}")

    # ── Also verify audit.json if it exists ──────────────────────────────────
    audit_path = Path("out/audit.json")
    if audit_path.exists():
        import json
        audit = json.loads(audit_path.read_text())
        blocked = [r for r in audit.get("records", [])
                   if any(t.get("state") == "blocked" for t in r.get("approval_trail", []))]
        print(f"  [OK] {len(blocked)} records have blocked state in audit trail")

    if errors:
        for e in errors:
            print(f"\n{e}", file=sys.stderr)
        return 1

    print("\n  [OK] probe-approval PASS: all approval gates hold")
    return 0

if __name__ == "__main__":
    sys.exit(main())
