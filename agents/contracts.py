"""
agents/contracts.py — Typed I/O contracts for every agent.

Each agent declares:
  - Input type  (what it accepts)
  - Output type (what it returns)
  - can_call    (which other agents it is permitted to invoke)

These contracts are the typed handoff boundaries that make this a real fleet,
not a god-function. Each can be tested in isolation.
"""
from __future__ import annotations

import hashlib
import json
from enum import Enum
from typing import Any, Literal, Optional
from pydantic import BaseModel, Field, field_validator


# ─────────────────────────────────────────────
# Enums
# ─────────────────────────────────────────────

class ReasonCode(str, Enum):
    # Data-layer Class-A (blocking)
    STALE              = "STALE"
    MISSING_INPUT      = "MISSING_INPUT"
    OUTLIER            = "OUTLIER"
    INJECTION_BLOCKED  = "INJECTION_BLOCKED"
    LOW_CONFIDENCE     = "LOW_CONFIDENCE"
    UNVERIFIED_ANOMALY = "UNVERIFIED_ANOMALY"
    # Agent-layer (also blocking)
    AGENT_HALLUCINATION = "AGENT_HALLUCINATION"
    AGENT_LOOP          = "AGENT_LOOP"
    AGENT_MALFORMED     = "AGENT_MALFORMED"
    BUDGET_EXCEEDED     = "BUDGET_EXCEEDED"
    # Class-B (auto-resolved, continues)
    SCHEMA_DRIFT        = "SCHEMA_DRIFT"
    SUPERSEDED_VERSION  = "SUPERSEDED_VERSION"


class ReasonClass(str, Enum):
    A = "A"   # Blocking — never reaches Delivery without human resolution
    B = "B"   # Auto-resolved — continues to Delivery (logged)


CLASS_A_CODES = {
    ReasonCode.STALE, ReasonCode.MISSING_INPUT, ReasonCode.OUTLIER,
    ReasonCode.INJECTION_BLOCKED, ReasonCode.LOW_CONFIDENCE,
    ReasonCode.UNVERIFIED_ANOMALY,
    ReasonCode.AGENT_HALLUCINATION, ReasonCode.AGENT_LOOP,
    ReasonCode.AGENT_MALFORMED, ReasonCode.BUDGET_EXCEEDED,
}

CLASS_B_CODES = {ReasonCode.SCHEMA_DRIFT, ReasonCode.SUPERSEDED_VERSION}


class ApprovalState(str, Enum):
    DRAFT             = "draft"
    IN_REVIEW         = "in_review"
    CHANGES_REQUESTED = "changes_requested"
    APPROVED          = "approved"
    DELIVERED         = "delivered"
    BLOCKED           = "blocked"


class VerifierVerdict(str, Enum):
    PASS        = "pass"
    FAIL        = "fail"
    NEEDS_HUMAN = "needs_human"


class AgentRole(str, Enum):
    ORCHESTRATOR = "orchestrator"
    WORKER       = "worker"
    VERIFIER     = "verifier"
    ROUTER       = "router"
    OPERATOR     = "operator"
    OTHER        = "other"


class TraceStatus(str, Enum):
    OK        = "ok"
    RETRIED   = "retried"
    REJECTED  = "rejected"
    OVERRULED = "overruled"
    ROUTED    = "routed"
    ABSTAINED = "abstained"
    KILLED    = "killed"


# ─────────────────────────────────────────────
# Core record types
# ─────────────────────────────────────────────

class RawRecord(BaseModel):
    """A record as parsed from source (feed.json, .eml, or .pdf). Pre-normalization."""
    id: str
    source_format: Literal["feed", "eml", "pdf"]
    source_hash: str            # sha256 of the raw source bytes/JSON
    raw_fields: dict[str, Any]  # All parsed fields, exactly as received
    version: int = 1


class NormalizedRecord(BaseModel):
    """A record after field normalization (field_map.yaml applied)."""
    id: str
    owner: Optional[str]
    deadline: Optional[str]
    amount: Optional[float]
    category: Optional[str]
    notes: Optional[str] = None
    version: int = 1
    source_format: Literal["feed", "eml", "pdf"]
    source_hash: str
    schema_drifts: list[str] = Field(default_factory=list)  # Fields that were renamed


class ExceptionRecord(BaseModel):
    """A record that has been routed to the exception queue."""
    id: str
    source_format: str
    reason_code: ReasonCode
    reason_class: ReasonClass
    detail: str
    version: int = 1
    source_hash: str = ""


class AgentSpan(BaseModel):
    """One agent step in the per-record trace."""
    agent: str
    model: Optional[str] = None
    prompt_version: Optional[str] = None
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None
    cost_usd: Optional[float] = None
    latency_ms: Optional[float] = None
    retries: Optional[int] = None
    transcript_hash: Optional[str] = None
    status: TraceStatus
    verdict: Optional[str] = None   # For Verifier spans: pass|fail|needs_human
    detail: Optional[str] = None


class ApprovalEntry(BaseModel):
    """One step in the approval trail."""
    state: ApprovalState
    actor: str
    ts: str
    reason: Optional[str] = None


# ─────────────────────────────────────────────
# Orchestrator contracts
# ─────────────────────────────────────────────

class OrchestratorInput(BaseModel):
    """What the Orchestrator receives to start a run."""
    records: list[NormalizedRecord]
    run_id: str
    pipeline_now: str           # ISO date string used for STALE checks
    max_cost_usd_per_record: float = 0.05
    max_steps_per_record: int = 5
    replay_llm: bool = True

    # Declared can_call (which agents Orchestrator may invoke)
    can_call: list[str] = Field(default=["Worker", "Verifier"])


class RecordRouting(BaseModel):
    """Orchestrator's per-record routing decision."""
    record_id: str
    action: Literal["assemble", "exception", "skip"]
    reason_code: Optional[ReasonCode] = None
    reason_class: Optional[ReasonClass] = None
    detail: Optional[str] = None
    steps_used: int = 0
    cost_usd: float = 0.0


class OrchestratorOutput(BaseModel):
    """What the Orchestrator returns after routing all records."""
    run_id: str
    routings: list[RecordRouting]
    total_cost_usd: float
    total_steps: int


# ─────────────────────────────────────────────
# Worker contracts
# ─────────────────────────────────────────────

class WorkerInput(BaseModel):
    """What the Worker agent receives per record."""
    record: NormalizedRecord
    model: str                    # Selected by ModelRouter
    prompt_version: str = "worker-v1"
    replay_llm: bool = True

    # Declared can_call (Worker is a leaf — calls no other agents)
    can_call: list[str] = Field(default=[])


class DeliveredFields(BaseModel):
    """Structured legal case brief fields produced by the Worker agent."""
    id: str
    attorney: str
    case_type: str
    normalized_claim_amount: float
    matter_classification: str
    priority_level: Literal["routine", "elevated", "urgent"]
    recommended_strategy: str
    case_summary: str
    law_firm_brand: str = "CEDX Legal Services - Case Management Division"
    pipeline_version: str = "1.0.0"
    generated_at: Optional[str] = None


class WorkerOutput(BaseModel):
    """What the Worker agent returns."""
    record_id: str
    abstain: bool = False
    abstain_reason: Optional[str] = None
    delivered_fields: Optional[DeliveredFields] = None
    # Trace metadata
    model: str = ""
    prompt_version: str = "worker-v1"
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    retries: int = 0
    transcript_hash: Optional[str] = None


# ─────────────────────────────────────────────
# Verifier contracts
# ─────────────────────────────────────────────

class VerifierInput(BaseModel):
    """What the Verifier receives: source + Worker draft."""
    source: NormalizedRecord
    worker_output: WorkerOutput
    allowed_delivered_fields: list[str]   # From output_schema.yaml
    prompt_version: str = "verifier-v1"
    replay_llm: bool = True

    # Declared can_call (Verifier is a leaf — independent check)
    can_call: list[str] = Field(default=[])


class VerifierOutput(BaseModel):
    """What the Verifier returns."""
    record_id: str
    verdict: VerifierVerdict
    reason_code: Optional[ReasonCode] = None  # Set when verdict != pass
    detail: str = ""
    # Trace metadata
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    latency_ms: float = 0.0
    transcript_hash: Optional[str] = None


# ─────────────────────────────────────────────
# Agent roster (for audit.json)
# ─────────────────────────────────────────────

class AgentRosterEntry(BaseModel):
    name: str
    role: AgentRole
    models: list[str]
    prompt_version: Optional[str] = None
    can_call: list[str]


AGENT_ROSTER: list[AgentRosterEntry] = [
    AgentRosterEntry(
        name="Orchestrator",
        role=AgentRole.ORCHESTRATOR,
        models=[],
        prompt_version="n/a",
        can_call=["Worker", "Verifier"],
    ),
    AgentRosterEntry(
        name="Worker",
        role=AgentRole.WORKER,
        models=["gpt-4o-mini", "gpt-4o", "claude-3-5-haiku-20241022", "gemini-1.5-flash"],
        prompt_version="worker-v1",
        can_call=[],
    ),
    AgentRosterEntry(
        name="Verifier",
        role=AgentRole.VERIFIER,
        models=["gpt-4o-mini", "gpt-4o", "claude-3-5-haiku-20241022", "gemini-1.5-flash"],
        prompt_version="verifier-v1",
        can_call=[],
    ),
]


# ─────────────────────────────────────────────
# Hashing utilities
# ─────────────────────────────────────────────

def canon(obj: Any) -> bytes:
    """Canonical JSON serialization for hashing — matches verify_audit.py."""
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def sha256_hex(obj: Any) -> str:
    """Return 'sha256:<hex>' of the canonical JSON of obj."""
    return "sha256:" + hashlib.sha256(canon(obj)).hexdigest()


def sha256_bytes(data: bytes) -> str:
    """Return 'sha256:<hex>' of raw bytes."""
    return "sha256:" + hashlib.sha256(data).hexdigest()
