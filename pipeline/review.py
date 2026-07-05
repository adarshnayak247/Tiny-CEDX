"""
pipeline/review.py — Stage 4: Approval state machine + CASE_ID amendment gate.

Approval state machine: draft → in_review → changes_requested → approved → delivered
Every state transition is appended to the record's approval_trail.

CASE_ID Amendment:
  Any record where normalized_claim_amount >= T needs an additional approval
  by role R, computed from sha256(CASE_ID):
    H = sha256(CASE_ID)
    R = ["risk_officer","legal_counsel","compliance","finance_controller"][int(H[0],16) % 4]
    T = 10000 + (int(H[1:3],16) % 50) * 1000

Server-side enforcement: delivery is refused for any non-approved item, refusal is logged.
"""
from __future__ import annotations

import hashlib
import os
import time
from typing import Optional

from agents.contracts import (
    ApprovalEntry, ApprovalState, NormalizedRecord, ReasonCode, WorkerOutput,
)

CASE_ID = os.getenv("CASE_ID", "CEDX-C2F18A")
AMENDMENT_ROLES = ["risk_officer", "legal_counsel", "compliance", "finance_controller"]

# Compute amendment parameters from CASE_ID
def _compute_amendment(case_id: str) -> tuple[str, float]:
    H = hashlib.sha256(case_id.encode()).hexdigest()
    role = AMENDMENT_ROLES[int(H[0], 16) % 4]
    threshold = 10000 + (int(H[1:3], 16) % 50) * 1000
    return role, float(threshold)

AMENDMENT_ROLE, AMENDMENT_THRESHOLD = _compute_amendment(CASE_ID)


class ApprovalStateMachine:
    """
    Per-record approval state machine.
    States: draft → in_review → [changes_requested →] approved → delivered

    Server-side: delivery is blocked if state != approved.
    Amendment gate: if amount >= AMENDMENT_THRESHOLD, a second approver with
    role AMENDMENT_ROLE is required before state can reach 'approved'.
    """

    TRANSITIONS = {
        ApprovalState.DRAFT:              {ApprovalState.IN_REVIEW},
        ApprovalState.IN_REVIEW:          {ApprovalState.APPROVED, ApprovalState.CHANGES_REQUESTED},
        ApprovalState.CHANGES_REQUESTED:  {ApprovalState.IN_REVIEW},
        ApprovalState.APPROVED:           {ApprovalState.DELIVERED, ApprovalState.BLOCKED},
        ApprovalState.DELIVERED:          set(),  # terminal
        ApprovalState.BLOCKED:            set(),  # terminal
    }

    def __init__(self, record_id: str, initial_amount: float = 0.0):
        self.record_id = record_id
        self.amount = initial_amount
        self.trail: list[ApprovalEntry] = []
        self.state = ApprovalState.DRAFT
        self._amendment_approved = False
        self._append(ApprovalState.DRAFT, actor="system", reason="Record initialized")

    def current_state(self) -> ApprovalState:
        return self.state

    def needs_amendment_approval(self) -> bool:
        """True if this record requires a second approver via CASE_ID amendment."""
        return self.amount >= AMENDMENT_THRESHOLD

    def transition(
        self, new_state: ApprovalState, actor: str, reason: Optional[str] = None
    ) -> tuple[bool, str]:
        """
        Attempt a state transition.
        Returns (success, message).
        """
        allowed = self.TRANSITIONS.get(self.state, set())
        if new_state not in allowed:
            msg = (
                f"Invalid transition {self.state.value} → {new_state.value} "
                f"for record {self.record_id}"
            )
            self._append(ApprovalState.BLOCKED, actor="system", reason=msg)
            return False, msg

        # Amendment gate: before APPROVED, check second approver was recorded
        if new_state == ApprovalState.APPROVED and self.needs_amendment_approval():
            if not self._amendment_approved:
                msg = (
                    f"Amendment requires approval by role={AMENDMENT_ROLE!r} "
                    f"(amount {self.amount} >= threshold {AMENDMENT_THRESHOLD}). "
                    f"Second approval not yet recorded."
                )
                self._append(ApprovalState.BLOCKED, actor="system", reason=msg)
                return False, msg

        self.state = new_state
        self._append(new_state, actor=actor, reason=reason)
        return True, f"Transitioned to {new_state.value}"

    def record_amendment_approval(self, actor: str, role: str) -> tuple[bool, str]:
        """Record the CASE_ID amendment second approval."""
        if role != AMENDMENT_ROLE:
            msg = f"Amendment approval requires role={AMENDMENT_ROLE!r}, got {role!r}"
            return False, msg
        self._amendment_approved = True
        self._append(
            self.state, actor=actor,
            reason=f"Amendment approval recorded: role={role}, threshold={AMENDMENT_THRESHOLD}"
        )
        return True, f"Amendment approval recorded for {actor} (role={role})"

    def can_deliver(self) -> tuple[bool, str]:
        """Server-side gate: refuse delivery if not approved."""
        if self.state != ApprovalState.APPROVED:
            msg = (
                f"DELIVERY REFUSED: record {self.record_id} is in state "
                f"{self.state.value!r}, not 'approved'. "
                f"Approval required before delivery."
            )
            return False, msg
        return True, "Approved for delivery"

    def get_trail(self) -> list[dict]:
        return [e.model_dump() for e in self.trail]

    def _append(
        self, state: ApprovalState, actor: str, reason: Optional[str] = None
    ) -> None:
        self.trail.append(ApprovalEntry(
            state=state,
            actor=actor,
            ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            reason=reason,
        ))


class ReviewStage:
    """
    Manages the approval state machines for all assembled records.
    In demo/replay mode, auto-approves clean records via 'system/demo' actor.
    In real mode, waits for CLI operator actions.
    """

    def __init__(self, demo_mode: bool = True):
        self.demo_mode = demo_mode
        self.machines: dict[str, ApprovalStateMachine] = {}

    def initialize(
        self,
        approved_outputs: dict[str, WorkerOutput],
        record_amounts: dict[str, float],
    ) -> None:
        """Create state machines for all assembled records."""
        for rec_id, worker_out in approved_outputs.items():
            amount = record_amounts.get(rec_id, 0.0)
            if worker_out.delivered_fields:
                amount = worker_out.delivered_fields.normalized_claim_amount
            self.machines[rec_id] = ApprovalStateMachine(rec_id, amount)

    def run_demo_approvals(self) -> dict[str, list[dict]]:
        """
        In demo mode, auto-advance all records through the approval chain.
        Records needing amendment approval get a synthetic second approver.
        Returns {record_id: trail}.
        """
        trails = {}
        for rec_id, machine in self.machines.items():
            # Move to in_review
            machine.transition(ApprovalState.IN_REVIEW, actor="system/demo")
            # Amendment approval if needed
            if machine.needs_amendment_approval():
                machine.record_amendment_approval(
                    actor=f"auto/{AMENDMENT_ROLE}",
                    role=AMENDMENT_ROLE,
                )
            # Approve
            machine.transition(ApprovalState.APPROVED, actor="system/demo")
            trails[rec_id] = machine.get_trail()
        return trails

    def get_approved_ids(self) -> list[str]:
        """Return IDs of records in 'approved' state."""
        return [
            rec_id for rec_id, m in self.machines.items()
            if m.state == ApprovalState.APPROVED
        ]

    def refuse_delivery(self, rec_id: str) -> tuple[bool, str]:
        """
        Attempt delivery of a non-approved record.
        Always returns (False, message) — logs the refusal.
        Used by probe-approval.
        """
        machine = self.machines.get(rec_id)
        if not machine:
            return False, f"Unknown record {rec_id}"
        ok, msg = machine.can_deliver()
        return ok, msg


def get_amendment_info() -> dict:
    """Return amendment info for audit.json."""
    return {
        "role": AMENDMENT_ROLE,
        "threshold": AMENDMENT_THRESHOLD,
        "case_id": CASE_ID,
        "formula": "R=[...][int(H[0],16)%4], T=10000+(int(H[1:3],16)%50)*1000 where H=sha256(CASE_ID)",
    }
