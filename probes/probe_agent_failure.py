#!/usr/bin/env python3
"""
probes/probe_agent_failure.py — make probe-agent-failure

Exit 0 ONLY if:
  1. A hallucinated Worker output (extra fields not in schema) is caught by Verifier → AGENT_HALLUCINATION
  2. A malformed Worker output (missing required fields) is caught → AGENT_MALFORMED
  3. Neither reaches delivery.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent))

from agents.contracts import (
    NormalizedRecord, ReasonCode, VerifierInput, VerifierVerdict, WorkerInput, WorkerOutput
)
from agents.contracts import DeliveredFields
from agents.verifier import VerifierAgent
from output_schema import ALLOWED_FIELDS


def _make_source() -> NormalizedRecord:
    return NormalizedRecord(
        id="PROBE-AGENT",
        owner="probe.user",
        deadline="2026-08-01",
        amount=5000.0,
        category="RENEWAL",
        notes="Probe test record",
        version=1,
        source_format="feed",
        source_hash="sha256:0000",
    )

def _make_clean_df() -> DeliveredFields:
    """Create a clean DeliveredFields matching the source record."""
    return DeliveredFields(
        id="PROBE-AGENT",
        attorney="probe.user",
        case_type="RENEWAL",
        normalized_claim_amount=5000.0,
        matter_classification="Retainer Renewal Review",
        priority_level="routine",
        recommended_strategy="Proceed with standard renewal protocols.",
        case_summary="Standard retainer renewal for probe.user. Claim amount $5,000.00.",
        law_firm_brand="CEDX Legal Services - Case Management Division",
        pipeline_version="1.0.0",
    )

def main() -> int:
    verifier = VerifierAgent()
    source = _make_source()
    errors = []

    clean_df = _make_clean_df()

    # ── Test 1: Hallucinated field ────────────────────────────────────────────
    print("[probe-agent-failure] Test 1: Hallucinated field")
    hallucinated_dict = clean_df.model_dump()
    hallucinated_dict["credit_score"] = 750       # HALLUCINATED
    hallucinated_dict["kyc_verified"] = True       # HALLUCINATED

    verdict, reason_code, detail = verifier._rule_check(source, hallucinated_dict, ALLOWED_FIELDS)
    if verdict != VerifierVerdict.FAIL:
        errors.append(f"FAIL: Hallucinated fields not caught (verdict={verdict})")
    elif reason_code != ReasonCode.AGENT_HALLUCINATION:
        errors.append(f"FAIL: Wrong reason code: expected AGENT_HALLUCINATION got {reason_code}")
    else:
        print(f"  [OK] Hallucinated fields caught: {detail[:80]}")

    # ── Test 2: ID mismatch (hallucination) ──────────────────────────────────
    print("[probe-agent-failure] Test 2: ID mismatch")
    id_mismatch_dict = clean_df.model_dump()
    id_mismatch_dict["id"] = "WRONG-ID"
    verdict2, code2, detail2 = verifier._rule_check(source, id_mismatch_dict, ALLOWED_FIELDS)
    if verdict2 != VerifierVerdict.FAIL or code2 != ReasonCode.AGENT_HALLUCINATION:
        errors.append(f"FAIL: ID mismatch not caught as hallucination")
    else:
        print(f"  [OK] ID mismatch caught: {detail2[:80]}")

    # ── Test 3: Amount mismatch ───────────────────────────────────────────────
    print("[probe-agent-failure] Test 3: Amount mismatch")
    amount_mismatch = clean_df.model_dump()
    amount_mismatch["normalized_claim_amount"] = 99999.0  # wrong amount
    verdict3, code3, detail3 = verifier._rule_check(source, amount_mismatch, ALLOWED_FIELDS)
    if verdict3 != VerifierVerdict.FAIL or code3 != ReasonCode.AGENT_HALLUCINATION:
        errors.append(f"FAIL: Amount mismatch not caught")
    else:
        print(f"  [OK] Amount mismatch caught: {detail3[:80]}")

    # ── Test 4: Missing required field (AGENT_MALFORMED) ─────────────────────
    print("[probe-agent-failure] Test 4: Missing required field")
    malformed_dict = clean_df.model_dump()
    malformed_dict["case_summary"] = None   # required field missing
    malformed_dict["recommended_strategy"] = None
    verdict4, code4, detail4 = verifier._rule_check(source, malformed_dict, ALLOWED_FIELDS)
    if verdict4 != VerifierVerdict.FAIL or code4 != ReasonCode.AGENT_MALFORMED:
        errors.append(f"FAIL: Missing required field not caught as AGENT_MALFORMED")
    else:
        print(f"  [OK] Missing required field caught: {detail4[:80]}")

    # ── Test 5: Clean output passes ────────────────────────────────────────────
    print("[probe-agent-failure] Test 5: Clean output passes")
    clean_dict = clean_df.model_dump()
    verdict5, code5, detail5 = verifier._rule_check(source, clean_dict, ALLOWED_FIELDS)
    if verdict5 != VerifierVerdict.PASS:
        errors.append(f"FAIL: Clean output rejected (verdict={verdict5}, reason={detail5})")
    else:
        print(f"  [OK] Clean output passes: {detail5[:80]}")

    if errors:
        for e in errors:
            print(f"\n{e}", file=sys.stderr)
        return 1

    print("\n  [OK] probe-agent-failure PASS: Verifier catches all bad Worker outputs")
    return 0

if __name__ == "__main__":
    sys.exit(main())
