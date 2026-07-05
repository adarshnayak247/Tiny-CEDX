#!/usr/bin/env python3
"""
cli.py — Operator CLI control surface.

Allows a human operator to inspect, approve, reject, edit-resolve records,
and transition them through the approval state machine (draft → in_review → approved → delivered).

Usage:
    python cli.py list
    python cli.py view <id>
    python cli.py approve <id> [--role <role_R>]
    python cli.py reject <id> [--reason <reason>]
    python cli.py edit-resolve <id> [--amount <amount>] [--deadline <YYYY-MM-DD>]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

# Ensure imports work from project root
sys.path.insert(0, str(Path(__file__).parent))

from agents.contracts import ApprovalState, ReasonCode, ReasonClass
from pipeline.review import ApprovalStateMachine, AMENDMENT_ROLE, AMENDMENT_THRESHOLD, CASE_ID
from pipeline.delivery import _write_json

AUDIT_PATH = Path(os.getenv("OUT_DIR", "out")) / "audit.json"


def _load_audit() -> dict:
    if not AUDIT_PATH.exists():
        print(f"ERROR: {AUDIT_PATH} not found. Run `make demo` first.", file=sys.stderr)
        sys.exit(1)
    return json.loads(AUDIT_PATH.read_text(encoding="utf-8"))


def _save_audit(audit: dict):
    # Atomic replace
    tmp_path = AUDIT_PATH.with_suffix(".tmp")
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(audit, f, indent=2, ensure_ascii=False)
    tmp_path.replace(AUDIT_PATH)


def cmd_list(args):
    audit = _load_audit()
    records = audit.get("records", [])

    print(f"\n  --- Record Roster (Total: {len(records)}) ---")
    print(f"  {'ID':<12} | {'Status':<12} | {'Reason Code':<20} | {'Claim Amt':<10} | {'Attorney':<12}")
    print("-" * 75)
    for r in records:
        df = r.get("delivered_fields") or {}
        amount = df.get("normalized_claim_amount") or "n/a"
        attorney = df.get("attorney") or "n/a"
        reason = r.get("reason_code") or "none"
        print(f"  {r['id']:<12} | {r['status']:<12} | {reason:<20} | {str(amount):<10} | {attorney:<12}")
    print()


def cmd_view(args):
    audit = _load_audit()
    record = next((r for r in audit.get("records", []) if r["id"] == args.id), None)
    if not record:
        print(f"ERROR: Record {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    print(f"\n============================================================")
    print(f"  RECORD: {record['id']} (Status: {record['status']})")
    print(f"============================================================")
    print(f"  Source Format: {record.get('source_format')}")
    print(f"  Version:       {record.get('version')}")
    if record.get("reason_code"):
        print(f"  Reason Code:   {record.get('reason_code')} ({record.get('reason_class')})")

    df = record.get("delivered_fields")
    if df:
        print(f"\n  Delivered Fields:")
        for k, v in df.items():
            print(f"    {k:<20}: {v}")
            
    print(f"\n  Approval Trail:")
    for step in record.get("approval_trail", []):
        print(f"    [{step['ts']}] {step['state']:<18} by {step['actor']:<18} : {step.get('reason') or ''}")

    print(f"\n  Agent Spans:")
    for span in record.get("agent_trace", []):
        print(f"    - {span['agent']:<15} {span['status']:<12} verdict={span.get('verdict')}")
    print()


def cmd_approve(args):
    audit = _load_audit()
    record = next((r for r in audit.get("records", []) if r["id"] == args.id), None)
    if not record:
        print(f"ERROR: Record {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    # Reconstruct state machine
    df = record.get("delivered_fields") or {}
    amount = df.get("normalized_claim_amount") or 0.0
    machine = ApprovalStateMachine(record["id"], amount)
    
    # Restore past trail to sync current state
    trail = record.get("approval_trail", [])
    if trail:
        machine.trail = []
        for step in trail:
            machine.trail.append(args.ApprovalEntry(**step) if hasattr(args, "ApprovalEntry") else step)
        machine.state = ApprovalState(trail[-1]["state"])

    # If it needs amendment approval, handle it
    if machine.needs_amendment_approval():
        if not args.role:
            print(f"ERROR: Record {args.id} requires amendment approval. Please specify --role.", file=sys.stderr)
            print(f"Required role: {AMENDMENT_ROLE} (Amount {amount} >= {AMENDMENT_THRESHOLD})", file=sys.stderr)
            sys.exit(1)
        ok, msg = machine.record_amendment_approval(actor="operator/human", role=args.role)
        print(f"  {msg}")
        if not ok:
            record["approval_trail"] = [t if isinstance(t, dict) else t.model_dump() for t in machine.trail]
            _save_audit(audit)
            sys.exit(1)

    # Transition to APPROVED
    ok, msg = machine.transition(ApprovalState.APPROVED, actor="operator/human", reason="Approved via CLI")
    print(f"  {msg}")
    
    # Sync back
    record["approval_trail"] = [t if isinstance(t, dict) else t.model_dump() for t in machine.trail]
    record["status"] = "delivered" if ok else record["status"]
    
    if ok:
        # Save delivered file under out/package
        pkg_dir = Path(os.getenv("OUT_DIR", "out")) / "package"
        pkg_dir.mkdir(parents=True, exist_ok=True)
        pkg_path = pkg_dir / f"{record['id']}.json"
        with open(pkg_path, "w", encoding="utf-8") as f:
            json.dump(df, f, indent=2, ensure_ascii=False)
        print(f"  [OK] Branded package saved: {pkg_path}")

    # Append audit complete event
    audit["events"].append({
        "seq": len(audit["events"]),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": "operator/human",
        "action": "approved",
        "record_id": record["id"]
    })
    
    _save_audit(audit)
    print("  [OK] audit.json updated.")


def cmd_reject(args):
    audit = _load_audit()
    record = next((r for r in audit.get("records", []) if r["id"] == args.id), None)
    if not record:
        print(f"ERROR: Record {args.id} not found.", file=sys.stderr)
        sys.exit(1)

    trail = record.get("approval_trail", [])
    if not trail:
        machine = ApprovalStateMachine(record["id"])
    else:
        df = record.get("delivered_fields") or {}
        machine = ApprovalStateMachine(record["id"], df.get("normalized_claim_amount", 0.0))
        machine.state = ApprovalState(trail[-1]["state"])
        machine.trail = trail

    reason = args.reason or "Rejected by case review operator"
    ok, msg = machine.transition(ApprovalState.CHANGES_REQUESTED, actor="operator/human", reason=reason)
    print(f"  {msg}")

    record["approval_trail"] = [t if isinstance(t, dict) else t.model_dump() for t in machine.trail]
    record["status"] = "exception"
    
    audit["events"].append({
        "seq": len(audit["events"]),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "actor": "operator/human",
        "action": "changes_requested",
        "record_id": record["id"]
    })
    _save_audit(audit)
    print("  [OK] audit.json updated.")


def main():
    parser = argparse.ArgumentParser(description="CEDX Legal Case Management Operator Console")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # list
    subparsers.add_parser("list", help="List all processed records")

    # view
    p_view = subparsers.add_parser("view", help="View record detail")
    p_view.add_argument("id", help="Record ID")

    # approve
    p_app = subparsers.add_parser("approve", help="Approve a record for delivery")
    p_app.add_argument("id", help="Record ID")
    p_app.add_argument("--role", help="Specify role if record requires amendment approval")

    # reject
    p_rej = subparsers.add_parser("reject", help="Reject/Request changes on a record")
    p_rej.add_argument("id", help="Record ID")
    p_rej.add_argument("--reason", help="Rejection reason")

    args = parser.parse_args()

    if args.command == "list":
        cmd_list(args)
    elif args.command == "view":
        cmd_view(args)
    elif args.command == "approve":
        cmd_approve(args)
    elif args.command == "reject":
        cmd_reject(args)


if __name__ == "__main__":
    main()
