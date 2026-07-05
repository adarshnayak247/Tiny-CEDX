#!/usr/bin/env python3
"""
main.py — Entry point for the CEDX Tiny Agent Fleet (Legal Services).

Usage:
    python main.py                    # Full pipeline run (REPLAY_LLM=true)
    python main.py --trace CASE-001   # Print agent trace for one case
    python main.py --replay CASE-001  # Print data lineage for one case
    python main.py --probe-approval   # Test approval gate (for make probe-approval)
    python main.py --probe-agent-failure  # Test Verifier catches bad Worker output
    python main.py --probe-budget     # Test BUDGET_EXCEEDED handling
    python main.py --probe-append-only    # Test audit append-only enforcement
    python main.py --probe-idempotency    # Run pipeline twice, check for dupes

One command for the full run: `make demo` → `python main.py`
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid
from pathlib import Path

# ── Ensure we can import from project root ───────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

from agents.contracts import ExceptionRecord, ReasonCode, ReasonClass
from pipeline.intake import IntakeStage
from pipeline.orchestration import OrchestrationStage
from pipeline.assembly import AssemblyStage
from pipeline.review import ReviewStage, AMENDMENT_ROLE, AMENDMENT_THRESHOLD, CASE_ID, get_amendment_info
from pipeline.delivery import DeliveryStage, verify_append_only

SEED_DIR = Path(os.getenv("SEED_DIR", "seed"))
OUT_DIR = Path(os.getenv("OUT_DIR", "out"))


def run_pipeline(run_id: str | None = None) -> int:
    """
    Run the full 5-stage legal case processing pipeline. Returns exit code (0 = success).
    """
    run_id = run_id or f"run-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"

    print(f"\n{'='*60}")
    print(f"  CEDX Legal Case Management Fleet  |  {run_id}")
    print(f"  CASE_ID: {CASE_ID}")
    print(f"  AMENDMENT: role={AMENDMENT_ROLE}  threshold={AMENDMENT_THRESHOLD:.0f}")
    print(f"  SEED_DIR: {SEED_DIR}")
    print(f"  REPLAY_LLM: {os.getenv('REPLAY_LLM', 'true')}")
    print(f"{'='*60}\n")

    # ── Stage 1: Intake ──────────────────────────────────────────────────────
    print("[1/5] INTAKE - parsing case filing documents...")
    intake = IntakeStage(seed_dir=SEED_DIR)
    raw_cases_all, superseded_events = intake.run()
    raw_cases = intake.load_from_db()
    superseded_ids = intake.get_superseded_ids()
    print(f"      {len(raw_cases_all)} cases ingested, {len(raw_cases)} active, {len(superseded_ids)} superseded")

    # ── Stage 2: Orchestration ───────────────────────────────────────────────
    print("[2/5] ORCHESTRATION - normalizing + exception detection...")
    orch_stage = OrchestrationStage()
    clean_cases, data_exceptions = orch_stage.run(raw_cases, superseded_ids)
    print(f"      {len(clean_cases)} clean, {len(data_exceptions)} data exceptions")
    for exc in data_exceptions:
        print(f"      X {exc.id:<12} {exc.reason_code.value:<25} {exc.detail[:60] if exc.detail else ''}")

    # ── Stage 3: Assembly (Orchestrator → Worker → Verifier) ─────────────────
    print(f"[3/5] ASSEMBLY - {len(clean_cases)} cases through agent fleet...")
    assembly = AssemblyStage()
    approved_outputs, agent_exceptions, traces = assembly.run(clean_cases, run_id)
    all_exceptions = data_exceptions + agent_exceptions

    print(f"      {len(approved_outputs)} assembled, {len(agent_exceptions)} agent exceptions")
    for exc in agent_exceptions:
        print(f"      X {exc.id:<12} {exc.reason_code.value:<25} {exc.detail[:60] if exc.detail else ''}")

    # ── Stage 4: Review ──────────────────────────────────────────────────────
    print(f"[4/5] REVIEW - case approval state machine for {len(approved_outputs)} cases...")
    review = ReviewStage(demo_mode=True)
    review.initialize(approved_outputs, {})
    approval_trails = review.run_demo_approvals()
    print(f"      {len(review.get_approved_ids())} cases approved for filing")

    # ── Stage 5: Delivery ─────────────────────────────────────────────────────
    print("[5/5] DELIVERY - writing case briefs + audit.json...")
    delivery = DeliveryStage(out_dir=OUT_DIR)
    pkg_hash = delivery.run(
        approved_outputs=approved_outputs,
        approval_trails=approval_trails,
        traces=traces,
        all_exceptions=all_exceptions,
        clean_records=clean_cases,
        superseded_ids=superseded_ids,
        seed_dir=str(SEED_DIR),
        run_id=run_id,
    )

    print(f"\n{'='*60}")
    print(f"  [OK] Pipeline complete")
    print(f"  [OK] {len(approved_outputs)} cases delivered, {len(all_exceptions)} exceptions")
    print(f"  [OK] out/audit.json + out/package/ written")
    print(f"  [OK] package_hash = {pkg_hash[:40]}...")
    print(f"{'='*60}\n")
    return 0


def cmd_trace(case_id: str) -> int:
    """Print the full agent decision path for one case from audit.json."""
    audit_path = OUT_DIR / "audit.json"
    if not audit_path.exists():
        print(f"ERROR: {audit_path} not found. Run `make demo` first.", file=sys.stderr)
        return 1
    audit = json.loads(audit_path.read_text())
    case = next((r for r in audit.get("records", []) if r.get("id") == case_id), None)
    if not case:
        print(f"ERROR: case {case_id!r} not found in audit.", file=sys.stderr)
        return 1

    print(f"\n{'='*60}")
    print(f"  AGENT TRACE - {case_id}")
    print(f"  Status: {case.get('status')}  |  Reason: {case.get('reason_code', 'none')}")
    print(f"{'='*60}")
    for i, span in enumerate(case.get("agent_trace", []), 1):
        print(f"\n  Span {i}: {span.get('agent')}")
        print(f"    model:        {span.get('model', 'n/a')}")
        print(f"    status:       {span.get('status')}")
        print(f"    verdict:      {span.get('verdict', 'n/a')}")
        print(f"    tokens_in:    {span.get('tokens_in') or 0}")
        print(f"    tokens_out:   {span.get('tokens_out') or 0}")
        print(f"    cost_usd:     ${span.get('cost_usd') or 0.0:.6f}")
        print(f"    latency_ms:   {span.get('latency_ms') or 0.0:.1f}ms")
        print(f"    retries:      {span.get('retries') or 0}")
        print(f"    tx_hash:      {(span.get('transcript_hash') or 'n/a')[:30]}")
        if span.get("detail"):
            print(f"    detail:       {span.get('detail')[:80]}")

    print(f"\n  Approval trail:")
    for step in case.get("approval_trail", []):
        print(f"    {step.get('state'):<20} by {step.get('actor'):<20} at {step.get('ts')}")
    print()
    return 0


def cmd_replay(case_id: str) -> int:
    """Reconstruct data lineage for a filed case from the audit log."""
    audit_path = OUT_DIR / "audit.json"
    if not audit_path.exists():
        print(f"ERROR: {audit_path} not found.", file=sys.stderr)
        return 1
    audit = json.loads(audit_path.read_text())
    case = next((r for r in audit.get("records", []) if r.get("id") == case_id), None)
    if not case:
        print(f"ERROR: case {case_id!r} not found.", file=sys.stderr)
        return 1

    print(f"\n{'='*60}")
    print(f"  DATA LINEAGE - {case_id}")
    print(f"{'='*60}")
    print(f"  Source format:      {case.get('source_format')}")
    print(f"  Source hash:        {(case.get('source_version_hash') or 'n/a')[:40]}")
    print(f"  Status:             {case.get('status')}")
    print(f"  Transcript hash:    {(case.get('transcript_hash') or 'n/a')[:40]}")
    print(f"  Delivered hash:     {(case.get('delivered_fields_hash') or 'n/a')[:40]}")

    # Find events for this case
    events = [e for e in audit.get("events", []) if e.get("record_id") == case_id]
    print(f"\n  Event log ({len(events)} events):")
    for ev in events:
        print(f"    [seq={ev.get('seq')}] {ev.get('action'):<25} by {ev.get('actor')}")

    print(f"\n  Agent chain:")
    for span in case.get("agent_trace", []):
        cost = span.get("cost_usd") or 0
        print(f"    {span.get('agent'):<15} -> {span.get('status'):<12} cost=${cost:.6f}")
    print()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="CEDX Tiny Agent Fleet")
    parser.add_argument("--trace", metavar="ID", help="Print agent trace for record ID")
    parser.add_argument("--replay", metavar="ID", help="Print data lineage for record ID")
    args = parser.parse_args()

    if args.trace:
        return cmd_trace(args.trace)
    if args.replay:
        return cmd_replay(args.replay)
    return run_pipeline()


if __name__ == "__main__":
    sys.exit(main())
