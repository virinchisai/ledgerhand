"""Human-in-the-loop: the control-transfer model.

The seam this implies (and the reason it is modelled explicitly rather than as
a boolean flag): automation must be able to pause mid-run, cede a *live*
session to a person, and resume on that same session afterwards. That only
works if "who is driving" is a first-class, single-valued piece of state.

So: one session has exactly one ControlLease. The automation holds it; to
escalate it releases it to an operator; on handback it reacquires with a new
token. A stale token cannot act -- which is what stops the classic failure
where automation resumes while a human is still typing.
"""
from __future__ import annotations

import secrets
from datetime import datetime, timezone
from enum import Enum

from pydantic import BaseModel, Field

from .enums import ControlOwner


def _now() -> datetime:
    return datetime.now(timezone.utc)


class InterventionTrigger(str, Enum):
    """Why we stopped. Determines routing and what the operator is asked to do."""
    #: Discovery loop made no progress / ran out of steps.
    AGENT_STUCK = "agent_stuck"
    #: Replay hit a declared hard failure.
    REPLAY_HARD_FAILURE = "replay_hard_failure"
    #: A recoverable condition exceeded its retry budget.
    RECOVERY_EXHAUSTED = "recovery_exhausted"
    #: Policy requires a person to approve this specific action.
    RISK_APPROVAL = "risk_approval"
    #: An unrecognised state -- nothing in the artifact describes what we see.
    UNKNOWN_STATE = "unknown_state"


class InterventionStatus(str, Enum):
    PENDING = "pending"        # raised, nobody has picked it up
    CLAIMED = "claimed"        # an operator has it, lease not yet transferred
    OPERATOR_ACTIVE = "operator_active"   # operator holds the live session
    RETURNED = "returned"      # operator handed control back
    RESOLVED = "resolved"      # run continued and finished
    ABANDONED = "abandoned"    # timed out / cancelled


class OperatorAction(BaseModel):
    """One thing the human did while holding the session.

    Captured so the run's evidence is complete across the handoff, and so a
    repeated manual fix can be promoted into the artifact later.
    """
    at: datetime = Field(default_factory=_now)
    kind: str                  # click | type | navigate | note | resume
    target: str = ""
    #: Redacted before it is written anywhere.
    value: str | None = None
    note: str = ""


class ControlLease(BaseModel):
    """Single-valued ownership of a live session."""
    owner: ControlOwner = ControlOwner.AGENT
    #: Presented on every act(); a holder with a stale token is refused.
    token: str = Field(default_factory=lambda: secrets.token_hex(8))
    acquired_at: datetime = Field(default_factory=_now)
    holder_label: str = "agent"

    def rotate(self, owner: ControlOwner, label: str) -> "ControlLease":
        return ControlLease(owner=owner, holder_label=label)


class InterventionRequest(BaseModel):
    """The packet an operator receives. Must be actionable on its own."""
    id: str = Field(default_factory=lambda: f"iv_{secrets.token_hex(5)}")
    created_at: datetime = Field(default_factory=_now)
    status: InterventionStatus = InterventionStatus.PENDING
    trigger: InterventionTrigger

    # -- what was being attempted --
    run_id: str
    session_id: str
    capability: str | None = None
    goal: str | None = None
    tenant: str = ""
    step_index: int | None = None
    step_intent: str | None = None

    # -- what we see right now --
    reason: str = ""
    url: str = ""
    observation_summary: str = ""
    screenshot: str | None = None
    #: What the operator is expected to accomplish before handing back.
    requested_of_operator: str = ""

    # -- lifecycle --
    operator: str | None = None
    claimed_at: datetime | None = None
    returned_at: datetime | None = None
    operator_actions: list[OperatorAction] = Field(default_factory=list)
    resolution: str | None = None
    #: Set by the operator on handback: continue the run, or abort it.
    resume_requested: bool = False
