"""Turning a successful discovery run into a capability artifact.

The recorder is where the system stops being an agent and starts being a
compiler. It takes a trace -- which is a record of one model's meandering --
and emits a contract: typed, parameterised, checkpointed, and stripped of
everything specific to the run that produced it.

Two things it works hard to avoid, because both produce artifacts that pass
their own replay and fail on the second invocation:

  * Baking run-specific data into locators or checkpoints. If the checkpoint
    for "we reached the detail screen" is the text `DANA WHITFIELD`, the
    capability works for exactly one member. Every candidate string is checked
    against the run's own parameter values and rejected if it leaked.
  * Recording incidental steps as mandatory. An interstitial that happened to
    appear during discovery is a *condition*, not a step. Those are detected
    and recorded as optional.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models.artifact import (
    CapabilityArtifact, CapabilityPolicy, ExtractionSpec, OutputSpec, Provenance,
    Step, TargetBinding, WaitSpec,
)
from ..models.conditions import Condition, all_of, text_present
from ..models.enums import ActionKind, ParamType, RiskTier, Sensitivity, SurfaceKind
from ..models.observation import Observation, UINode
from ..products.profile import ProductProfile
from ..replay.conditions import evaluate
from ..replay.locators import describe_control, normalize
from .loop import DiscoveryResult, TraceStep

_MONEY = re.compile(r"^-?[$]?\s?[\d,]+\.\d{2}$")
_NUMERIC = re.compile(r"\d")

#: Screen text too generic to prove we reached a particular state.
_WEAK_CHECKPOINT_TEXT = {
    "home", "back", "cancel", "clear", "submit", "ok", "continue", "search",
    "authorized use only. activity is monitored.",
}


@dataclass
class RecorderConfig:
    capability_version: int = 1
    #: Outcome codes from the product profile to attach. None = all of them.
    outcome_codes: list[str] | None = None
    approval_required_for_unattended: bool = True


def record(
    result: DiscoveryResult,
    product: ProductProfile,
    *,
    config: RecorderConfig | None = None,
) -> CapabilityArtifact:
    """Compile a successful DiscoveryResult into a CapabilityArtifact."""
    if not result.succeeded:
        raise ValueError(f"cannot record a run with status {result.status!r}")
    cfg = config or RecorderConfig()
    goal = result.goal
    forbidden = {v for v in goal.param_values.values() if v}

    kept = _prune(result.trace, product)
    steps: list[Step] = []
    for position, (trace_step, after) in enumerate(_with_after(kept, result)):
        steps.append(_to_step(position, trace_step, after, forbidden, product))

    final_obs = result.final_observation
    outputs = [
        _to_output(want.name, want.description, want.type, want.sensitivity,
                   node, final_obs)
        for want in goal.outputs
        if (node := result.output_bindings.get(want.name)) is not None
    ]

    success = _success_condition(final_obs, outputs, forbidden)
    if success is not None:
        # The last step's checkpoint and the success condition are the same
        # assertion; keeping one avoids asserting the same thing twice with
        # two chances to disagree.
        if steps:
            steps[-1].checkpoint = success
            steps[-1].wait.until = success

    return CapabilityArtifact(
        id=goal.capability_id,
        version=cfg.capability_version,
        name=goal.capability_name,
        description=goal.description or goal.goal,
        target=TargetBinding(
            product=product.product,
            product_version=goal.product_version,
            tenant=goal.tenant,
            surface=SurfaceKind.WEB,
            entry_url=goal.entry_url,
        ),
        inputs=list(goal.params),
        outputs=outputs,
        outcomes=product.applicable(cfg.outcome_codes),
        steps=steps,
        success_condition=success,
        policy=CapabilityPolicy(
            max_risk=max((s.risk for s in steps),
                         key=lambda r: [RiskTier.SAFE, RiskTier.ELEVATED,
                                        RiskTier.IRREVERSIBLE].index(r),
                         default=RiskTier.SAFE),
            requires_approval_for_unattended=cfg.approval_required_for_unattended,
        ),
        provenance=Provenance(
            discovery_run_id=result.run_id,
            model=result.model,
            raw_step_count=len(result.trace),
            surface_fingerprint=surface_fingerprint(final_obs),
            notes=(f"recorded from goal {goal.goal!r}; "
                   f"{len(result.trace)} model steps pruned to {len(steps)}"),
        ),
    )


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def _prune(trace: list[TraceStep], product: ProductProfile) -> list[TraceStep]:
    """Keep the steps that actually moved the flow forward.

    Failed actions are dropped: they are evidence, not instructions. A repeated
    action against an unchanged screen is dropped for the same reason.
    """
    kept: list[TraceStep] = []
    for ts in trace:
        if ts.action in (None, ActionKind.FINISH):
            continue
        if not ts.ok:
            continue
        if kept:
            prev = kept[-1]
            same = (prev.action == ts.action
                    and (prev.node.handle if prev.node else None) == (ts.node.handle if ts.node else None)
                    and (prev.value_ref.describe() if prev.value_ref else None)
                        == (ts.value_ref.describe() if ts.value_ref else None))
            if same and prev.url_after == ts.url_after:
                continue
        kept.append(ts)
    return kept


def _with_after(
    kept: list[TraceStep], result: DiscoveryResult
) -> list[tuple[TraceStep, Observation | None]]:
    """Pair each kept step with the observation that followed it.

    The observation a later step saw *is* the outcome of the earlier step, so
    checkpoints come from the trace rather than from a second pass over the app.
    """
    by_index = {ts.index: ts for ts in result.trace}
    ordered = sorted(by_index)
    pairs: list[tuple[TraceStep, Observation | None]] = []
    for ts in kept:
        nxt = next((by_index[i].observation for i in ordered if i > ts.index), None)
        pairs.append((ts, nxt or result.final_observation))
    return pairs


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def _to_step(
    position: int,
    ts: TraceStep,
    after: Observation | None,
    forbidden: set[str],
    product: ProductProfile,
) -> Step:
    target = None
    if ts.node is not None:
        target = describe_control(ts.node, ts.observation)

    checkpoint = _derive_checkpoint(ts.observation, after, forbidden)

    # A step taken while a recoverable condition was on screen is a reaction to
    # that condition, not part of the flow. Record it, but as optional.
    optional = False
    note = ""
    for outcome in product.outcomes:
        if outcome.recovery and evaluate(outcome.detect, ts.observation).value:
            optional = True
            note = f" (handles {outcome.code})"
            break

    intent = (ts.decision.reason if ts.decision else "") or f"{ts.action.value} step"
    return Step(
        index=position,
        intent=(intent + note).strip()[:140],
        action=ts.action or ActionKind.CLICK,
        target=target,
        value=ts.value_ref if ts.action in (ActionKind.TYPE, ActionKind.SELECT) else None,
        key=ts.decision.key if ts.decision and ts.action is ActionKind.PRESS else None,
        wait=WaitSpec(settle=True, until=checkpoint),
        checkpoint=checkpoint,
        risk=ts.risk,
        optional=optional,
    )


def _derive_checkpoint(
    before: Observation, after: Observation | None, forbidden: set[str]
) -> Condition | None:
    """Assert on what newly appeared -- if it is chrome rather than data.

    This is the subtle one. A checkpoint must hold for *every* invocation, so
    it has to be page furniture ("Member Detail"), never record content
    ("MAIN", "DANA WHITFIELD"). Both arrive as new text on the same screen, so
    the filters do the separating:

      * hard rejects -- the run's own parameter values, anything containing a
        digit (ids, balances, dates, confirmation numbers), generic chrome that
        appears on every screen, and anything rendered as a control's value;
      * scoring -- chrome is title-cased, often multi-word, and appears near
        the top of the page; record data on these screens is upper-cased and
        appears below the headings.

    Casing and position are heuristics, and they are the weakest link in the
    recorder. They are backed by two things rather than trusted alone: the
    success condition independently requires every output control to resolve,
    and the checkpoint is written into the artifact in plain text where a
    reviewer sees it before the capability is approved.
    """
    if after is None:
        return None
    seen = {normalize(t) for t in before.texts}
    # Anything rendered as a control's value is data by definition.
    data_strings = {normalize(n.value) for n in after.controls if n.value}
    data_strings |= {normalize(n.text) for n in after.controls if n.attrs.get("col_header")}

    scored: list[tuple[int, int, str]] = []
    total = max(len(after.texts), 1)
    for position, line in enumerate(after.texts):
        text = line.strip()
        n = normalize(text)
        if not n or n in seen or n in _WEAK_CHECKPOINT_TEXT or n in data_strings:
            continue
        if len(text) < 3 or len(text) > 48:
            continue
        if _NUMERIC.search(text):
            continue
        if any(f and f in text for f in forbidden):
            continue

        score = 0
        if normalize(after.title) and n in normalize(after.title):
            score += 3
        has_lower = any(c.islower() for c in text)
        has_upper = any(c.isupper() for c in text)
        if has_upper and not has_lower:
            score -= 3          # SHOUTED tokens on these screens are field values
        elif has_upper and has_lower:
            score += 2          # Title Case reads as a heading
        if " " in text:
            score += 1
        ratio = position / total
        score += 2 if ratio < 0.25 else (1 if ratio < 0.5 else 0)
        scored.append((score, position, text))

    if not scored:
        return None
    best = max(scored, key=lambda t: (t[0], -t[1]))
    if best[0] <= 0:
        # Nothing on this screen is safe to assert on. Saying so is better than
        # writing a checkpoint that will fail for the second caller.
        return None
    return text_present(best[2], note="state reached after this step")


#: Roles whose label is a label. A cell's "label" falls back to its own text,
#: which is the customer's data -- including it makes the fingerprint change
#: for every member and turns the drift signal into noise.
_LABELLED_ROLES = frozenset({"textbox", "button", "combobox", "listbox",
                             "checkbox", "radio", "link", "menuitem"})


def surface_fingerprint(obs: Observation | None) -> str:
    """A hash of stable screen markers, recorded so replay can notice drift.

    Built from the labels of *interactive* controls only. The first version of
    this hashed every node's label, and since a content cell reports its own
    text as its label, the fingerprint changed whenever the member did -- which
    reported drift on a capability that was working perfectly. A drift signal
    that cries wolf is worse than none.
    """
    import hashlib
    if obs is None:
        return ""
    labels = sorted({normalize(n.label) for n in obs.controls
                     if n.label and n.role in _LABELLED_ROLES})
    return hashlib.sha256("|".join(labels).encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Outputs and success
# ---------------------------------------------------------------------------

def _to_output(
    name: str, description: str, ptype: ParamType, sensitivity: Sensitivity,
    node: UINode, obs: Observation | None,
) -> OutputSpec:
    text = node.text or node.value
    inferred = ptype
    transform: str = "trim"
    if _MONEY.match(text.strip()):
        inferred = ParamType.MONEY if ptype is ParamType.STRING else ptype
        transform = "money"
    return OutputSpec(
        name=name,
        type=inferred,
        description=description,
        sensitivity=sensitivity,
        extract=ExtractionSpec(
            control=describe_control(
                node, obs or Observation(step=0, controls=[node]),
                # Named for the contract, so a sensitive value cannot become
                # the human-readable description of where it was read from.
                description=f"source of output {name!r}"),
            attribute="value" if (node.value and not node.text) else "text",
            transform=transform,  # type: ignore[arg-type]
        ),
    )


def _success_condition(
    obs: Observation | None, outputs: list[OutputSpec], forbidden: set[str]
) -> Condition | None:
    """Assert the goal state, not just the last click.

    Composed of a screen marker plus the presence of every control an output is
    read from -- so 'success' means the data is actually there to return, which
    is what the caller cares about.
    """
    if obs is None:
        return None
    parts: list[Condition] = []
    marker = _derive_checkpoint(Observation(step=0), obs, forbidden)
    if marker is not None:
        parts.append(marker)
    for out in outputs:
        parts.append(Condition(kind="control_present", control=out.extract.control,
                               note=f"output {out.name} is readable"))
    if not parts:
        return None
    return parts[0] if len(parts) == 1 else all_of(*parts, note="goal state reached")
