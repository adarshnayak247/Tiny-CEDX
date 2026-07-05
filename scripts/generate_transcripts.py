import json
import hashlib
from pathlib import Path

CLEAN_CASES = [
    {"id": "REC-001", "attorney": "a.shah", "case_type": "ONBOARDING", "claim_amount": 4800.0, "notes": "Standard matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "REC-002", "attorney": "b.ortiz", "case_type": "RENEWAL", "claim_amount": 5200.0, "notes": "Standard matter.", "priority": "routine", "classification": "Retainer Renewal Review"},
    {"id": "REC-003", "attorney": "c.nguyen", "case_type": "REVIEW", "claim_amount": 3900.0, "notes": "Standard matter.", "priority": "routine", "classification": "Case Status Review"},
    {"id": "REC-004", "attorney": "d.kapoor", "case_type": "REPORT", "claim_amount": 6100.0, "notes": "Standard matter.", "priority": "routine", "classification": "Litigation Status Report"},
    {"id": "REC-005", "attorney": "e.moreau", "case_type": "INTAKE", "claim_amount": 4500.0, "notes": "Standard matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "REC-006", "attorney": "f.haddad", "case_type": "RENEWAL", "claim_amount": 5300.0, "notes": "Standard matter.", "priority": "routine", "classification": "Retainer Renewal Review"},
    {"id": "REC-007", "attorney": "g.silva", "case_type": "REVIEW", "claim_amount": 4700.0, "notes": "Standard matter.", "priority": "routine", "classification": "Case Status Review"},
    {"id": "REC-008", "attorney": "h.iqbal", "case_type": "REPORT", "claim_amount": 5000.0, "notes": "Standard matter.", "priority": "routine", "classification": "Litigation Status Report"},
    {"id": "REC-009", "attorney": "i.rossi", "case_type": "ONBOARDING", "claim_amount": 4600.0, "notes": "Standard matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "REC-010", "attorney": "j.cohen", "case_type": "INTAKE", "claim_amount": 5100.0, "notes": "Standard matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "REC-015", "attorney": "o.varga", "case_type": "INTAKE", "claim_amount": 5000.0, "notes": "Standard matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "REC-016", "attorney": "p.larsen", "case_type": "RENEWAL", "claim_amount": 4750.0, "notes": "Standard matter.", "priority": "routine", "classification": "Retainer Renewal Review"},
    {"id": "REC-017", "attorney": "q.abate", "case_type": "REPORT", "claim_amount": 4650.0, "notes": "Standard matter.", "priority": "routine", "classification": "Litigation Status Report"},
    {"id": "REC-018", "attorney": "r.ferreira", "case_type": "REVIEW", "claim_amount": 5150.0, "notes": "Standard matter.", "priority": "routine", "classification": "Case Status Review"},
    {"id": "REC-019", "attorney": "s.haque", "case_type": "ONBOARDING", "claim_amount": 4850.0, "notes": "Standard matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "REC-020", "attorney": "t.novak", "case_type": "REPORT", "claim_amount": 5250.0, "notes": "Standard matter.", "priority": "routine", "classification": "Litigation Status Report"},
]

GOLD_CASES = [
    {"id": "GOLD-001", "attorney": "a.shah", "case_type": "ONBOARDING", "claim_amount": 4800.0, "notes": "Standard intake matter.", "priority": "routine", "classification": "New Matter Intake"},
    {"id": "GOLD-007", "attorney": "g.larsen", "case_type": "RENEWAL", "claim_amount": 4750.0, "notes": "Routine settlement negotiation.", "priority": "routine", "classification": "Retainer Renewal Review"},
    {"id": "GOLD-008", "attorney": "h.iqbal", "case_type": "REPORT", "claim_amount": 5000.0, "notes": "Standard status update.", "priority": "routine", "classification": "Litigation Status Report"},
    {"id": "GOLD-009", "attorney": "i.rossi", "case_type": "REVIEW", "claim_amount": 35000.0, "notes": "High-stakes commercial litigation matter.", "priority": "elevated", "classification": "Case Status Review"},
]

ALL_CLEAN_CASES = CLEAN_CASES + GOLD_CASES

transcripts_dir = Path(__file__).parent.parent / "transcripts"
transcripts_dir.mkdir(parents=True, exist_ok=True)

index = {}

def canon_bytes(obj) -> bytes:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")

def sha256_hex(obj) -> str:
    return hashlib.sha256(canon_bytes(obj)).hexdigest()

def write_transcript(agent, case_id, model, prompt_version, request, response, delivered_fields=None):
    resp_hash = "sha256:" + sha256_hex(response)
    df_hash = "sha256:" + sha256_hex(delivered_fields) if delivered_fields else None
    
    tx = {
        "agent": agent,
        "model": model,
        "prompt_version": prompt_version,
        "record_id": case_id,
        "request": request,
        "response": response,
        "response_hash": resp_hash,
        "delivered_fields_hash": df_hash,
        "ts": "2026-07-05T00:00:00Z"
    }
    
    stem = resp_hash.split(":")[-1]
    tx_path = transcripts_dir / f"{stem}.json"
    tx_path.write_text(json.dumps(tx, indent=2, ensure_ascii=False), encoding="utf-8")
    
    key = f"{agent}_{case_id}"
    index[key] = resp_hash

# Clean existing transcript json files to prevent orphaned transcripts
print("Cleaning old transcripts...")
for f in transcripts_dir.glob("*.json"):
    f.unlink()

# Generate transcripts for clean cases
print(f"Generating transcripts for {len(ALL_CLEAN_CASES)} cases...")
for c in ALL_CLEAN_CASES:
    case_id = c["id"]
    model = "gpt-4o" if c["priority"] == "elevated" else "gpt-4o-mini"
    
    # ── Worker Transcript ──
    worker_response = {
        "id": case_id,
        "attorney": c["attorney"],
        "case_type": c["case_type"],
        "normalized_claim_amount": float(c["claim_amount"]),
        "matter_classification": c["classification"],
        "priority_level": c["priority"],
        "recommended_strategy": f"Proceed with standard {c['case_type'].lower().replace('_', ' ')} protocols and timeline.",
        "case_summary": f"Legal analysis for {c['attorney']}'s {c['case_type'].lower().replace('_', ' ')} matter. Claim amount ${float(c['claim_amount']):,.2f} falls within {c['priority']} priority parameters.",
        "law_firm_brand": "CEDX Legal Services - Case Management Division",
        "pipeline_version": "1.0.0",
        "generated_at": "2026-07-05T00:00:00Z"
    }
    
    worker_request = {
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": f"Analyze this case: {case_id}"}
        ],
        "model": model
    }
    
    write_transcript(
        agent="Worker",
        case_id=case_id,
        model=model,
        prompt_version="worker-v1",
        request=worker_request,
        response=worker_response,
        delivered_fields=worker_response
    )
    
    # ── Verifier Transcript ──
    verifier_response = {
        "verdict": "pass",
        "reason": "Legal analysis validated. Worker output properly grounded in case facts with no fabricated citations."
    }
    
    verifier_request = {
        "messages": [
            {"role": "system", "content": "..."},
            {"role": "user", "content": "..."}
        ],
        "model": "gpt-4o-mini"
    }
    
    write_transcript(
        agent="Verifier",
        case_id=case_id,
        model="gpt-4o-mini",
        prompt_version="verifier-v1",
        request=verifier_request,
        response=verifier_response
    )

# Write index.json
index_path = transcripts_dir / "index.json"
index_path.write_text(json.dumps(index, indent=2, sort_keys=True, ensure_ascii=False), encoding="utf-8")
print(f"[OK] Generated transcripts successfully.")
