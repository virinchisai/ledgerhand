"""The capability artifact: what a discovery run produces and replay consumes.

Design stance, stated once because everything below follows from it:

  A capability is an *API contract that happens to be implemented by driving a
  UI*. So it is shaped like an API -- typed inputs, typed outputs, a declared
  set of outcomes it can return, and a version -- and not like a macro
  recording. The step list is an implementation detail of that contract.

Three consequences show up in the types:
  * values are never inlined when they are sensitive (see ValueRef),
  * "no such member" is a declared outcome, not an exception (see OutcomeSpec),
  * per-tenant difference is an overlay on one capability, not a second
    capability (see TenantOverlay).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from .conditions import Condition
from .enums import (
    ActionKind,
    ApprovalState,
    OutcomeClass,
    ParamType,
    RiskTier,
    Sensitivity,
    SurfaceKind,
)
from .locator import ControlDescriptor

SCHEMA_VERSION = "1.1"


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# Values
# ---------------------------------------------------------------------------

class ValueRef(BaseModel):
    """Where a step gets the text it types.

    The whole point of this type is that `secret` and `param` carry a *name*,
    never a value. An artifact is a reviewable, checked-in document; regulated
    data and credentials must not be able to end up in one even by accident.
    """
    source: Literal["literal", "param", "secret", "output"]
    #: source=literal -- safe constants only (see validator).
    literal: str | None = None
    #: source=param/secret/output -- the name to resolve at invocation time.
    ref: str | None = None

    @model_validator(mode="after")
    def _check(self) -> "ValueRef":
        if self.source == "literal" and self.literal is None:
            raise ValueError("literal ValueRef needs .literal")
        if self.source != "literal" and not self.ref:
            raise ValueError(f"{self.source} ValueRef needs .ref")
        if self.source == "literal" and self.ref:
            raise ValueError("literal ValueRef must not carry .ref")
        return self

    def describe(self) -> str:
        if self.source == "literal":
            return repr(self.literal)
        return f"@{self.source}:{self.ref}"


class ParamSpec(BaseModel):
    """One typed input the calling agent supplies per invocation."""
    name: str
    type: ParamType = ParamType.STRING
    required: bool = True
    description: str = ""
    #: Drives redaction. A PII/SECRET param's value never reaches a log or artifact.
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    default: Any | None = None
    pattern: str | None = None
    enum: list[str] | None = None
    minimum: float | None = None
    maximum: float | None = None
    #: Safe to show a reviewer. Must not be a real value for sensitive params.
    example: str | None = None

    def json_schema(self) -> dict[str, Any]:
        """JSON Schema fragment, so the capability can be published as a tool."""
        base_types = {
            ParamType.STRING: "string", ParamType.INTEGER: "integer",
            ParamType.NUMBER: "number", ParamType.BOOLEAN: "boolean",
            ParamType.ENUM: "string", ParamType.MONEY: "number",
        }
        out: dict[str, Any] = {"type": base_types[self.type]}
        if self.description:
            out["description"] = self.description
        if self.enum:
            out["enum"] = self.enum
        if self.pattern:
            out["pattern"] = self.pattern
        if self.minimum is not None:
            out["minimum"] = self.minimum
        if self.maximum is not None:
            out["maximum"] = self.maximum
        if self.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
            out["x-sensitivity"] = self.sensitivity.value
        return out


class ExtractionSpec(BaseModel):
    """How to pull one value off the screen."""
    control: ControlDescriptor
    attribute: Literal["text", "value", "attr"] = "text"
    attr_name: str | None = None
    #: Post-processing. `money` exists because "4,182.55" is not a number and
    #: the caller asked for a balance, not a string that looks like one.
    transform: Literal["none", "trim", "money", "integer", "regex"] = "trim"
    regex: str | None = None
    group: int = 1


class OutputSpec(BaseModel):
    """One typed value the capability returns."""
    name: str
    type: ParamType = ParamType.STRING
    description: str = ""
    sensitivity: Sensitivity = Sensitivity.INTERNAL
    required: bool = True
    extract: ExtractionSpec

    def json_schema(self) -> dict[str, Any]:
        base_types = {
            ParamType.STRING: "string", ParamType.INTEGER: "integer",
            ParamType.NUMBER: "number", ParamType.BOOLEAN: "boolean",
            ParamType.ENUM: "string", ParamType.MONEY: "number",
        }
        out: dict[str, Any] = {"type": base_types[self.type]}
        if self.description:
            out["description"] = self.description
        if self.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
            out["x-sensitivity"] = self.sensitivity.value
        return out


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

class WaitSpec(BaseModel):
    """Determinism lever.

    Replay waits on *conditions*, not on clocks. `before_ms` exists only for
    surfaces that genuinely have no observable settle signal; using it is
    recorded so it shows up in review.
    """
    before_ms: int = 0
    #: Wait for the surface's own quiescence signal (web: load + network idle).
    settle: bool = True
    timeout_ms: int = 12_000
    #: Explicit condition to wait for. Preferred over everything above.
    until: Condition | None = None


class Step(BaseModel):
    index: int
    #: Why this step exists, in a reviewer's language. Recorded from the model's
    #: stated reason at discovery, then kept as documentation.
    intent: str
    action: ActionKind
    target: ControlDescriptor | None = None
    value: ValueRef | None = None
    #: NAVIGATE only. May contain {param} placeholders.
    url: str | None = None
    #: PRESS only, e.g. "Enter".
    key: str | None = None
    #: EXTRACT only -- which declared output this fills.
    output: str | None = None
    wait: WaitSpec = Field(default_factory=WaitSpec)
    #: Asserted after the action. A step without a checkpoint is a step that
    #: cannot tell you it failed, so the recorder tries hard to attach one.
    checkpoint: Condition | None = None
    risk: RiskTier = RiskTier.SAFE
    #: Steps that handle conditions which may or may not occur (interstitials).
    optional: bool = False

    def describe(self) -> str:
        bits = [f"{self.index:>2}. {self.action.value}"]
        if self.target:
            bits.append(f"-> {self.target.description}")
        if self.url:
            bits.append(f"-> {self.url}")
        if self.value:
            bits.append(f"= {self.value.describe()}")
        if self.risk is not RiskTier.SAFE:
            bits.append(f"[{self.risk.value}]")
        return " ".join(bits)


class RecoverySpec(BaseModel):
    """What to do about a recoverable condition before trying again."""
    steps: list[Step] = Field(default_factory=list)
    #: Re-run the step that tripped the detector after recovery.
    retry_step: bool = True
    max_attempts: int = 2


class OutcomeSpec(BaseModel):
    """A thing that can happen, declared up front with how to recognise it.

    Every non-success exit of a capability is one of these. Callers can branch
    on `code` without string-matching an error message, and a reviewer can read
    the list and know what the capability can tell them.
    """
    code: str
    classification: OutcomeClass
    detect: Condition
    #: Caller-facing message. May reference {param} names.
    message: str = ""
    #: RECOVERABLE only.
    recovery: RecoverySpec | None = None
    #: Restrict checking to certain steps; None = check after every step.
    applies_to_steps: list[int] | None = None

    @model_validator(mode="after")
    def _check(self) -> "OutcomeSpec":
        if self.classification is OutcomeClass.RECOVERABLE and self.recovery is None:
            raise ValueError(f"recoverable outcome {self.code} must declare a recovery")
        return self


# ---------------------------------------------------------------------------
# Binding, policy, provenance
# ---------------------------------------------------------------------------

class TargetBinding(BaseModel):
    """What this capability was recorded against."""
    product: str                     # vendor product, shared across tenants
    product_version: str
    tenant: str
    surface: SurfaceKind = SurfaceKind.WEB
    #: Entry point. May contain {param} placeholders.
    entry_url: str
    #: Name of the policy profile that must be in force to run this.
    policy_profile: str = "default"


class TenantOverlay(BaseModel):
    """Per-tenant specialisation of one base capability.

    Hundreds of tenants run the same vendor product with different labels and
    routes. Re-recording per tenant would mean hundreds of artifacts drifting
    apart independently. An overlay is a small, reviewable diff against the base
    -- the base stays the single source of truth for the flow's *shape*.
    """
    tenant: str
    product_version: str | None = None
    #: "steps[2].target" -> replacement descriptor.
    control_overrides: dict[str, ControlDescriptor] = Field(default_factory=dict)
    #: "entry_url" or "steps[0].url" -> replacement template.
    url_overrides: dict[str, str] = Field(default_factory=dict)
    #: Outcome detectors whose wording differs per tenant.
    outcome_overrides: dict[str, Condition] = Field(default_factory=dict)
    notes: str = ""


class CapabilityPolicy(BaseModel):
    max_risk: RiskTier = RiskTier.ELEVATED
    #: IRREVERSIBLE capabilities do not run unattended without this cleared.
    requires_approval_for_unattended: bool = True
    allowed_actions: list[ActionKind] = Field(
        default_factory=lambda: list(ActionKind)
    )


class Provenance(BaseModel):
    """How this artifact came to exist. Deliberately *not* the transcript.

    The model transcript is evidence, kept under /evidence and referenced by id.
    Keeping it out of the artifact is what makes the artifact reviewable and
    keeps model chatter (which can quote screen contents, i.e. PII) out of a
    document that gets checked into a repo.
    """
    discovery_run_id: str
    recorded_at: datetime = Field(default_factory=_now)
    model: str = "unknown"
    #: Steps the model took before pruning; the gap is a signal of loop quality.
    raw_step_count: int = 0
    #: Hash over stable surface markers; a mismatch at replay means drift.
    surface_fingerprint: str = ""
    notes: str = ""


class StabilityRecord(BaseModel):
    """Replay history. Feeds the approval gate and a flakiness signal."""
    replays: int = 0
    successes: int = 0
    business_outcomes: int = 0
    failures: int = 0
    last_replay_at: datetime | None = None
    #: locator kind -> how often replay had to fall back to it.
    fallback_counts: dict[str, int] = Field(default_factory=dict)

    @property
    def success_rate(self) -> float | None:
        decided = self.successes + self.failures
        return None if decided == 0 else self.successes / decided


# ---------------------------------------------------------------------------
# The artifact
# ---------------------------------------------------------------------------

class CapabilityArtifact(BaseModel):
    """A versioned, reviewable, agent-invocable capability."""
    schema_version: str = SCHEMA_VERSION
    #: Stable dotted id, shared across versions and tenants.
    id: str
    version: int = 1
    name: str
    #: Written for two readers: a human reviewer and a calling model.
    description: str

    target: TargetBinding
    inputs: list[ParamSpec] = Field(default_factory=list)
    outputs: list[OutputSpec] = Field(default_factory=list)
    outcomes: list[OutcomeSpec] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    #: Asserted at the end. If this does not hold, the run did not succeed,
    #: whatever the individual steps reported.
    success_condition: Condition | None = None

    policy: CapabilityPolicy = Field(default_factory=CapabilityPolicy)
    approval: ApprovalState = ApprovalState.DRAFT
    provenance: Provenance
    stability: StabilityRecord = Field(default_factory=StabilityRecord)
    overlays: list[TenantOverlay] = Field(default_factory=list)

    # -- derived views ------------------------------------------------------

    @property
    def ref(self) -> str:
        return f"{self.id}@v{self.version}"

    @property
    def max_step_risk(self) -> RiskTier:
        order = {RiskTier.SAFE: 0, RiskTier.ELEVATED: 1, RiskTier.IRREVERSIBLE: 2}
        return max((s.risk for s in self.steps), key=lambda r: order[r], default=RiskTier.SAFE)

    def param(self, name: str) -> ParamSpec | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output(self, name: str) -> OutputSpec | None:
        return next((o for o in self.outputs if o.name == name), None)

    def outcome(self, code: str) -> OutcomeSpec | None:
        return next((o for o in self.outcomes if o.code == code), None)

    def tool_schema(self) -> dict[str, Any]:
        """Publish as a function/tool definition an AI agent can call by name."""
        required = [p.name for p in self.inputs if p.required]
        return {
            "name": self.id.replace(".", "_"),
            "description": (
                f"{self.description}\n\n"
                f"Returns: {', '.join(o.name for o in self.outputs) or '(none)'}. "
                f"Possible outcomes: {', '.join(o.code for o in self.outcomes) or '(none)'}."
            ),
            "input_schema": {
                "type": "object",
                "properties": {p.name: p.json_schema() for p in self.inputs},
                "required": required,
            },
        }

    def fingerprint(self) -> str:
        """Hash of the flow's *shape* -- changes when the capability changes,
        not when stability counters tick."""
        shape = {
            "id": self.id,
            "steps": [
                {"a": s.action.value, "t": s.target.description if s.target else None,
                 "u": s.url, "v": s.value.describe() if s.value else None}
                for s in self.steps
            ],
            "inputs": [p.name for p in self.inputs],
            "outputs": [o.name for o in self.outputs],
        }
        return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:16]

    # -- tenant specialisation ---------------------------------------------

    def overlay_for(self, tenant: str) -> TenantOverlay | None:
        return next((o for o in self.overlays if o.tenant == tenant), None)

    def resolve_for(self, tenant: str) -> "CapabilityArtifact":
        """Apply a tenant overlay and return a concrete, runnable capability.

        Replay always runs a *resolved* artifact, so the engine never has to
        know that overlays exist.
        """
        overlay = self.overlay_for(tenant)
        if overlay is None:
            return self
        spec = self.model_copy(deep=True)
        spec.target = spec.target.model_copy(update={
            "tenant": tenant,
            "product_version": overlay.product_version or spec.target.product_version,
            "entry_url": overlay.url_overrides.get("entry_url", spec.target.entry_url),
        })
        for path, descriptor in overlay.control_overrides.items():
            _apply_control_override(spec, path, descriptor)
        for path, url in overlay.url_overrides.items():
            if path.startswith("steps["):
                idx = int(path[len("steps["):path.index("]")])
                spec.steps[idx].url = url
        for code, cond in overlay.outcome_overrides.items():
            oc = spec.outcome(code)
            if oc:
                oc.detect = cond
        spec.provenance.notes = (
            f"{spec.provenance.notes} | resolved for tenant={tenant}".strip(" |")
        )
        return spec


def _apply_control_override(spec: CapabilityArtifact, path: str, descriptor: ControlDescriptor) -> None:
    """Supports 'steps[N].target', 'outputs[name].extract.control'."""
    if path.startswith("steps["):
        idx = int(path[len("steps["):path.index("]")])
        rest = path.split(".", 1)[1] if "." in path else "target"
        if rest == "target":
            spec.steps[idx].target = descriptor
            return
    if path.startswith("outputs["):
        name = path[len("outputs["):path.index("]")]
        out = spec.output(name)
        if out:
            out.extract.control = descriptor
            return
    raise ValueError(f"unsupported override path: {path!r}")
