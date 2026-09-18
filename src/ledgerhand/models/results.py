"""The replay result contract -- what a calling agent actually gets back.

The single most important property: a capability that ran correctly and found
nothing is NOT a failure. `status=business_outcome` with `outcome_code` is a
successful call whose answer happens to be "no such member". Callers branch on
a code; they never parse a message.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from .enums import LocatorKind, OutcomeClass, ReplayStatus


def _now() -> datetime:
    return datetime.now(timezone.utc)


class LocatorResolution(BaseModel):
    """Which hypothesis actually found the control. The drift signal."""
    control: str
    #: None when nothing resolved it -- including the degenerate case of a
    #: descriptor carrying no strategies at all, which must report a clean
    #: failure rather than crash the run that was trying to debug it.
    resolved_by: LocatorKind | None = None
    rank: int
    candidates_seen: int
    #: True when rank > 0, i.e. the preferred strategy no longer works.
    used_fallback: bool = False
    note: str | None = None


class StepReport(BaseModel):
    index: int
    intent: str
    action: str
    status: str  # ok | skipped | recovered | failed
    started_at: datetime = Field(default_factory=_now)
    duration_ms: int = 0
    locator: LocatorResolution | None = None
    checkpoint: str | None = None
    checkpoint_passed: bool | None = None
    #: Recoverable conditions handled inside this step.
    recoveries: list[str] = Field(default_factory=list)
    detail: str | None = None
    screenshot: str | None = None


class FailureDetail(BaseModel):
    """Everything needed to debug without re-running."""
    step_index: int
    step_intent: str
    expected: str
    observed: str
    #: Which locator strategies were tried and why each was rejected.
    locator_attempts: list[str] = Field(default_factory=list)
    screenshot: str | None = None
    dom_snapshot: str | None = None
    url: str | None = None


class ReplayResult(BaseModel):
    capability: str
    tenant: str
    run_id: str
    status: ReplayStatus
    #: Populated on SUCCESS. Sensitive outputs are redacted in logs, not here --
    #: this object is returned to the caller in-process, never written raw.
    outputs: dict[str, Any] = Field(default_factory=dict)
    #: Populated on BUSINESS_OUTCOME.
    outcome_code: str | None = None
    outcome_class: OutcomeClass | None = None
    message: str | None = None
    #: Populated on FAILED.
    failure: FailureDetail | None = None
    #: Populated on ESCALATED.
    intervention_id: str | None = None

    steps: list[StepReport] = Field(default_factory=list)
    started_at: datetime = Field(default_factory=_now)
    duration_ms: int = 0
    evidence_dir: str | None = None
    #: True when any control needed a lower-ranked locator than recorded.
    drift_detected: bool = False
    drift_notes: list[str] = Field(default_factory=list)

    @property
    def ok(self) -> bool:
        """A call that did its job -- including one that returned a business
        outcome. Distinct from `status is SUCCESS`, which means it also got
        the outputs."""
        return self.status in (ReplayStatus.SUCCESS, ReplayStatus.BUSINESS_OUTCOME)

    def summary(self) -> str:
        if self.status is ReplayStatus.SUCCESS:
            return f"SUCCESS {self.capability} -> {list(self.outputs)}"
        if self.status is ReplayStatus.BUSINESS_OUTCOME:
            return f"BUSINESS_OUTCOME {self.outcome_code}: {self.message}"
        if self.status is ReplayStatus.ESCALATED:
            return f"ESCALATED intervention={self.intervention_id}"
        f = self.failure
        return (f"FAILED at step {f.step_index} ({f.step_intent}): "
                f"expected {f.expected}; observed {f.observed}") if f else "FAILED"
