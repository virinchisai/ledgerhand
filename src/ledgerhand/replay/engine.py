"""Deterministic replay: the path an AI agent actually invokes in production.

No model is consulted here, ever. Given an artifact and typed arguments, the
engine resolves controls, acts, asserts, and returns a structured result.

The design question that matters is not "how do we repeat the clicks" -- that
part is easy. It is "what do we do when the screen is not what we recorded",
and the answer is that the engine never asks that question generically. After
every step it evaluates the capability's *declared* outcomes first, and only if
none of them match does it fall through to checkpoint evaluation. That ordering
is what makes "no such member" return as an answer instead of surfacing as a
checkpoint failure, and it is the single most important line of code here.
"""
from __future__ import annotations

import os
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..evidence.recorder import EvidenceWriter
from ..models.artifact import (
    CapabilityArtifact, ExtractionSpec, OutcomeSpec, ParamSpec, Step, ValueRef,
)
from ..models.enums import (
    ActionKind, ApprovalState, OutcomeClass, ParamType, ReplayStatus, RiskTier, Sensitivity,
)
from ..models.intervention import InterventionRequest, InterventionTrigger
from ..models.observation import Observation, UINode
from ..models.results import FailureDetail, LocatorResolution, ReplayResult, StepReport
from ..safety.policy import PolicyGate, Verdict
from ..safety.redaction import Redactor
from ..surface.base import ActRequest, Surface
from .conditions import evaluate
from .locators import resolve

_TEMPLATE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


class ArgumentError(Exception):
    """The caller's arguments do not satisfy the capability's input contract."""


@dataclass
class _Stop:
    """Internal control-flow signal: stop the run with this result shape."""
    status: ReplayStatus
    outcome: OutcomeSpec | None = None
    failure: FailureDetail | None = None


@dataclass
class ReplayEngine:
    surface: Surface
    gate: PolicyGate
    evidence: EvidenceWriter
    redactor: Redactor = field(default_factory=Redactor)
    #: False means "production, nobody is watching" -- irreversible steps then
    #: require an approval that no human is present to give, so they escalate.
    attended: bool = False
    #: Optional; when present, hard failures raise an intervention instead of
    #: simply returning FAILED.
    broker: Any = None
    session_id: str = ""
    #: Per-control retry when a locator does not resolve on the first look.
    locator_retries: int = 1
    #: How many times a run may be resumed after a human handoff.
    max_resumes: int = 1
    #: How long to hold the session open waiting for an operator.
    handoff_timeout_s: float = 900.0

    # -- public -------------------------------------------------------------

    def run(
        self,
        artifact: CapabilityArtifact,
        arguments: dict[str, Any],
        *,
        tenant: str | None = None,
    ) -> ReplayResult:
        started = time.monotonic()
        spec = artifact.resolve_for(tenant) if tenant else artifact
        run_id = self.evidence.run_id
        result = ReplayResult(
            capability=spec.ref, tenant=spec.target.tenant, run_id=run_id,
            status=ReplayStatus.FAILED, evidence_dir=str(self.evidence.dir),
        )

        self.evidence.event("replay_start", capability=spec.ref, tenant=spec.target.tenant,
                            approval=spec.approval.value, attended=self.attended,
                            arguments=sorted(arguments), policy_profile=self.gate.profile.name)

        try:
            args = self._validate_arguments(spec, arguments)
        except ArgumentError as exc:
            result.failure = FailureDetail(
                step_index=-1, step_intent="argument validation",
                expected="arguments satisfying the capability input contract",
                observed=str(exc))
            self.evidence.event("replay_end", status="failed", reason=str(exc))
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        # Register every sensitive argument before anything can be logged.
        for param in spec.inputs:
            if param.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
                self.redactor.register(str(args.get(param.name, "")), param.sensitivity.value)

        gate_failure = self._preflight(spec)
        if gate_failure is not None:
            result.failure = gate_failure
            self.evidence.event("replay_end", status="failed", reason=gate_failure.observed)
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result

        entry = _template(spec.target.entry_url, args)
        decision = self.gate.check_url(entry)
        if not decision.allowed:
            result.failure = FailureDetail(
                step_index=-1, step_intent="entry", expected="entry url inside the allowlist",
                observed=decision.reason, url=entry)
            self.evidence.event("policy_denied", scope="url", reason=decision.reason, url=entry)
            result.duration_ms = int((time.monotonic() - started) * 1000)
            return result
        self.surface.navigate(entry)
        self.surface.settle()

        try:
            stop = self._attempt(spec, args, result, start=0)
            stop = self._escalate_and_resume(spec, args, result, stop)
        except ArgumentError as exc:
            stop = _Stop(ReplayStatus.FAILED, failure=FailureDetail(
                step_index=-1, step_intent="value resolution",
                expected="every referenced parameter and secret to resolve",
                observed=str(exc)))
        self._apply_stop(spec, result, stop, args)
        result.duration_ms = int((time.monotonic() - started) * 1000)
        self.evidence.event("replay_end", status=result.status.value,
                            outcome=result.outcome_code, summary=result.summary(),
                            drift=result.drift_detected)
        self.evidence.write_json("result.json", result)
        return result

    # -- argument contract --------------------------------------------------

    @staticmethod
    def _validate_arguments(spec: CapabilityArtifact, arguments: dict[str, Any]) -> dict[str, Any]:
        """Enforce the declared input contract before touching the surface.

        Rejecting bad arguments here rather than letting them flow into the UI
        is what keeps "you passed a letter where a member id goes" from
        arriving at the caller disguised as a validation error from the bank.
        """
        out: dict[str, Any] = {}
        unknown = set(arguments) - {p.name for p in spec.inputs}
        if unknown:
            raise ArgumentError(f"unknown argument(s): {', '.join(sorted(unknown))}")
        for param in spec.inputs:
            if param.name not in arguments or arguments[param.name] in (None, ""):
                if param.required and param.default is None:
                    raise ArgumentError(f"missing required argument {param.name!r}")
                out[param.name] = param.default
                continue
            out[param.name] = _coerce(param, arguments[param.name])
        return out

    def _preflight(self, spec: CapabilityArtifact) -> FailureDetail | None:
        """Approval and risk ceiling, checked before any action is taken."""
        needs_approval = (spec.policy.requires_approval_for_unattended
                          and spec.max_step_risk is RiskTier.IRREVERSIBLE)
        if needs_approval and not self.attended and spec.approval is not ApprovalState.APPROVED:
            reason = (f"capability {spec.ref} contains an irreversible step and is "
                      f"{spec.approval.value}; unattended replay requires approval")
            self.evidence.event("policy_denied", scope="approval", reason=reason)
            return FailureDetail(step_index=-1, step_intent="preflight",
                                 expected="approved capability for unattended replay",
                                 observed=reason)
        if spec.approval is ApprovalState.REVOKED:
            return FailureDetail(step_index=-1, step_intent="preflight",
                                 expected="a capability that is not revoked",
                                 observed=f"{spec.ref} is revoked")
        return None

    # -- the step loop ------------------------------------------------------

    def _execute_steps(
        self, spec: CapabilityArtifact, args: dict[str, Any], result: ReplayResult,
        *, start_index: int = 0,
    ) -> _Stop | None:
        for step in spec.steps:
            if step.index < start_index:
                continue
            started = time.monotonic()
            report = StepReport(index=step.index, intent=step.intent,
                                action=step.action.value, status="ok")

            if step.wait.before_ms:
                time.sleep(step.wait.before_ms / 1000)

            obs = self.surface.perceive(step.index)

            # Declared outcomes are checked BEFORE anything else. Recoverable
            # ones may clear and let the same step proceed.
            stop, obs, recovered = self._handle_outcomes(spec, step, obs, result, report)
            if stop is not None:
                result.steps.append(report)
                return stop
            report.recoveries.extend(recovered)

            outcome = self._perform(spec, step, obs, args, result, report)
            if isinstance(outcome, _Stop):
                report.status = "failed"
                report.duration_ms = int((time.monotonic() - started) * 1000)
                result.steps.append(report)
                return outcome
            if outcome == "skipped":
                report.status = "skipped"
                report.duration_ms = int((time.monotonic() - started) * 1000)
                result.steps.append(report)
                continue

            self._wait_after(step)

            after = self.surface.perceive(step.index)
            stop, after, more = self._handle_outcomes(spec, step, after, result, report)
            if stop is not None:
                result.steps.append(report)
                return stop
            report.recoveries.extend(more)

            if step.checkpoint is not None:
                verdict = evaluate(step.checkpoint, after, self.surface.page_text())
                report.checkpoint = step.checkpoint.describe()
                report.checkpoint_passed = verdict.value
                if not verdict.value:
                    # Nothing declared matched and the state is not what we
                    # recorded: this is a genuine hard failure.
                    report.status = "failed"
                    report.duration_ms = int((time.monotonic() - started) * 1000)
                    result.steps.append(report)
                    return _Stop(ReplayStatus.FAILED, failure=self._failure(
                        step, expected=step.checkpoint.describe(),
                        observed=verdict.detail, obs=after))
            report.duration_ms = int((time.monotonic() - started) * 1000)
            result.steps.append(report)
        return None

    def _attempt(
        self, spec: CapabilityArtifact, args: dict[str, Any], result: ReplayResult, *, start: int
    ) -> _Stop:
        stop = self._execute_steps(spec, args, result, start_index=start)
        return stop if stop is not None else self._finalise(spec, result)

    def _escalate_and_resume(
        self, spec: CapabilityArtifact, args: dict[str, Any],
        result: ReplayResult, stop: _Stop,
    ) -> _Stop:
        """Park the run, let a human take the live session, then carry on.

        This is the whole point of the handoff being a *pause* rather than an
        abort: control goes away and comes back, and the run picks up at the
        step that stopped it -- on the same session, with the state the operator
        left behind. wait_for_handback blocks here while servicing the
        operator's commands, because this thread owns the surface.
        """
        if self.broker is None:
            return stop
        resumes = 0
        while (stop.status in (ReplayStatus.FAILED, ReplayStatus.ESCALATED)
               and resumes < self.max_resumes):
            request = self._escalate(spec, result, stop)
            if request is None:
                return stop
            result.intervention_id = request.id
            returned = self.broker.wait_for_handback(
                request.id, timeout_s=self.handoff_timeout_s)
            result.steps.append(StepReport(
                index=-1, intent="human intervention", action="handoff",
                status="recovered" if returned.resume_requested else "failed",
                detail=(f"operator {returned.operator or 'unassigned'} performed "
                        f"{len(returned.operator_actions)} action(s); "
                        f"{'resume' if returned.resume_requested else 'abort'} requested"),
            ))
            self.evidence.event("handoff_complete", intervention=request.id,
                                resume=returned.resume_requested,
                                operator=returned.operator,
                                actions=len(returned.operator_actions))
            if not returned.resume_requested:
                return _Stop(ReplayStatus.ESCALATED, failure=stop.failure)
            resumes += 1
            restart = stop.failure.step_index if stop.failure and stop.failure.step_index >= 0 else 0
            stop = self._attempt(spec, args, result, start=restart)
            self.broker.resolve(request.id,
                                f"run resumed from step {restart}; final status {stop.status.value}")
        return stop

    def _perform(
        self, spec: CapabilityArtifact, step: Step, obs: Observation,
        args: dict[str, Any], result: ReplayResult, report: StepReport,
    ) -> _Stop | str:
        """Resolve, gate and execute one step."""
        if step.action is ActionKind.NAVIGATE:
            url = _template(step.url or "", args)
            decision = self.gate.check_url(url)
            if not decision.allowed:
                return _Stop(ReplayStatus.FAILED, failure=self._failure(
                    step, expected="url inside the allowlist",
                    observed=decision.reason, obs=obs))
            self.surface.navigate(url)
            return "ok"

        if step.action in (ActionKind.WAIT, ActionKind.ASSERT, ActionKind.FINISH):
            return "ok"

        node: UINode | None = None
        if step.target is not None:
            node, resolution = self._resolve_with_retry(step, obs)
            report.locator = resolution
            if resolution and resolution.used_fallback:
                result.drift_detected = True
                note = (f"step {step.index} ({step.intent}): resolved by "
                        f"{resolution.resolved_by.value} at rank {resolution.rank}, "
                        f"not the recorded primary strategy")
                result.drift_notes.append(note)
                self.evidence.event("drift", step=step.index, note=note)
            if node is None:
                if step.optional:
                    # Optional steps exist precisely because the condition they
                    # handle usually is not present. Absence is expected.
                    return "skipped"
                attempts = resolution.note.split(" | ") if resolution and resolution.note else []
                return _Stop(ReplayStatus.FAILED, failure=self._failure(
                    step, expected=f"control {step.target.description}",
                    observed="no control matched any recorded strategy",
                    obs=obs, attempts=attempts))

        action_decision = self.gate.check_action(step.action)
        if not action_decision.allowed:
            return _Stop(ReplayStatus.FAILED, failure=self._failure(
                step, expected="an action permitted by the policy profile",
                observed=action_decision.reason, obs=obs))

        risk = step.risk if step.risk is not RiskTier.SAFE else self.gate.classify_risk(step.action, node)
        # An approved capability carries a standing human decision for its
        # irreversible steps -- that is what approving it *means*. Without this,
        # approval would gate entry to the run and then the run would escalate
        # at the very step approval was granted for, which makes the gate
        # ceremony rather than control. Unapproved and unattended still stops.
        decided_by_human = self.attended or spec.approval is ApprovalState.APPROVED
        risk_decision = self.gate.check_risk(risk, attended=decided_by_human)
        if risk_decision.verdict is Verdict.DENY:
            return _Stop(ReplayStatus.FAILED, failure=self._failure(
                step, expected="an action permitted at this risk tier",
                observed=risk_decision.reason, obs=obs))
        if risk_decision.verdict is Verdict.CONFIRM:
            self.evidence.event("confirmation_required", step=step.index,
                                risk=risk.value, control=step.target.description if step.target else "")
            return _Stop(ReplayStatus.ESCALATED, failure=self._failure(
                step, expected="human confirmation for an irreversible step",
                observed=risk_decision.reason, obs=obs))

        try:
            value, sensitive = self._value_for(step.value, args, spec.inputs)
        except ArgumentError as exc:
            # A secret that is not configured is an operational error the caller
            # must be able to read, not a stack trace. It reaches here rather
            # than preflight because a step's reference is only resolved when
            # that step runs.
            return _Stop(ReplayStatus.FAILED, failure=self._failure(
                step, expected="every referenced secret to be configured",
                observed=str(exc), obs=obs))
        outcome = self.surface.act(ActRequest(
            kind=step.action, node=node, value=value, key=step.key, sensitive=sensitive))
        self.evidence.event("action", step=step.index, action=step.action.value,
                            control=step.target.description if step.target else step.key,
                            ok=outcome.ok, detail=outcome.detail, error=outcome.error,
                            risk=risk.value,
                            locator=report.locator.resolved_by.value if report.locator else None)
        if not outcome.ok:
            return _Stop(ReplayStatus.FAILED, failure=self._failure(
                step, expected=f"{step.action.value} to succeed",
                observed=outcome.error or "action failed", obs=obs))
        return "ok"

    def _resolve_with_retry(
        self, step: Step, obs: Observation
    ) -> tuple[UINode | None, LocatorResolution | None]:
        """Resolve, and on a miss settle once and look again.

        A control that is not there yet is the commonest transient condition on
        these apps; retrying once is cheap and removes most of the flakiness a
        fixed sleep would otherwise be papering over.
        """
        assert step.target is not None
        attempts: list[str] = []
        for attempt in range(self.locator_retries + 1):
            res = resolve(step.target, obs)
            attempts.extend(res.attempts)
            if res.ok and res.node and res.strategy:
                return res.node, LocatorResolution(
                    control=step.target.description, resolved_by=res.strategy.kind,
                    rank=res.rank, candidates_seen=res.candidates,
                    used_fallback=res.used_fallback,
                    note=" | ".join(attempts))
            if attempt < self.locator_retries:
                self.surface.settle(timeout_ms=step.wait.timeout_ms)
                obs = self.surface.perceive(step.index)
        return None, LocatorResolution(
            control=step.target.description,
            resolved_by=step.target.primary.kind if step.target.primary else None,
            rank=-1, candidates_seen=0, used_fallback=False, note=" | ".join(attempts))

    def _wait_after(self, step: Step) -> None:
        """Wait on a condition where one was recorded; settle otherwise."""
        if step.wait.settle:
            self.surface.settle(timeout_ms=step.wait.timeout_ms)
        if step.wait.until is None:
            return
        deadline = time.monotonic() + step.wait.timeout_ms / 1000
        while time.monotonic() < deadline:
            obs = self.surface.perceive(step.index)
            if evaluate(step.wait.until, obs, self.surface.page_text()).value:
                return
            time.sleep(0.15)

    # -- outcome taxonomy ---------------------------------------------------

    def _handle_outcomes(
        self, spec: CapabilityArtifact, step: Step, obs: Observation,
        result: ReplayResult, report: StepReport,
    ) -> tuple[_Stop | None, Observation, list[str]]:
        """Evaluate declared outcomes, clearing recoverable ones in place.

        Returns (stop-or-None, possibly-refreshed observation, recoveries run).
        """
        recovered: list[str] = []
        for _ in range(3):  # bounded: a recovery that keeps re-firing is a failure
            page_text = self.surface.page_text()
            hit: OutcomeSpec | None = None
            for outcome in spec.outcomes:
                if outcome.applies_to_steps and step.index not in outcome.applies_to_steps:
                    continue
                if evaluate(outcome.detect, obs, page_text).value:
                    hit = outcome
                    break
            if hit is None:
                return None, obs, recovered

            self.evidence.event("outcome_detected", step=step.index, code=hit.code,
                                classification=hit.classification.value, url=obs.url)

            if hit.classification is OutcomeClass.BUSINESS:
                return _Stop(ReplayStatus.BUSINESS_OUTCOME, outcome=hit), obs, recovered

            if hit.classification is OutcomeClass.HARD:
                return (_Stop(ReplayStatus.FAILED, outcome=hit, failure=self._failure(
                    step, expected="the recorded state",
                    observed=f"{hit.code}: {hit.message}", obs=obs)), obs, recovered)

            # Recoverable: clear it and look again.
            assert hit.recovery is not None
            if recovered.count(hit.code) >= hit.recovery.max_attempts:
                return (_Stop(ReplayStatus.FAILED, outcome=hit, failure=self._failure(
                    step, expected=f"{hit.code} to clear after recovery",
                    observed=f"{hit.code} still present after "
                             f"{hit.recovery.max_attempts} attempts", obs=obs)), obs, recovered)
            recovered.append(hit.code)
            self.evidence.event("recovery", step=step.index, code=hit.code,
                                steps=len(hit.recovery.steps))
            for rstep in hit.recovery.steps:
                res = resolve(rstep.target, obs) if rstep.target else None
                if res is None or not res.ok:
                    break
                self.surface.act(ActRequest(kind=rstep.action, node=res.node))
                self.surface.settle()
            obs = self.surface.perceive(step.index)
        return None, obs, recovered

    # -- finish -------------------------------------------------------------

    def _finalise(self, spec: CapabilityArtifact, result: ReplayResult) -> _Stop:
        obs = self.surface.perceive(999)
        if spec.success_condition is not None:
            verdict = evaluate(spec.success_condition, obs, self.surface.page_text())
            if not verdict.value:
                return _Stop(ReplayStatus.FAILED, failure=FailureDetail(
                    step_index=len(spec.steps), step_intent="success condition",
                    expected=spec.success_condition.describe(), observed=verdict.detail,
                    url=obs.url,
                    screenshot=self.surface.screenshot(self.evidence.screen_path("failure")),
                    dom_snapshot=self.surface.snapshot(self.evidence.snapshot_path("failure"))))
        fingerprint = _fingerprint(obs)
        recorded = spec.provenance.surface_fingerprint
        if recorded and fingerprint != recorded:
            note = (f"surface fingerprint changed since recording "
                    f"({recorded} -> {fingerprint}); labels on this screen have moved")
            result.drift_notes.append(note)
            self.evidence.event("drift", note=note)
        return _Stop(ReplayStatus.SUCCESS)

    def _apply_stop(
        self, spec: CapabilityArtifact, result: ReplayResult, stop: _Stop, args: dict[str, Any]
    ) -> None:
        result.status = stop.status
        if stop.outcome is not None:
            result.outcome_code = stop.outcome.code
            result.outcome_class = stop.outcome.classification
            result.message = _template(stop.outcome.message, args)
        if stop.failure is not None:
            result.failure = stop.failure
        if stop.status is ReplayStatus.SUCCESS:
            obs = self.surface.perceive(1000)
            result.outputs = self._extract(spec, obs, result)
        if result.intervention_id and stop.status is not ReplayStatus.SUCCESS:
            # A run that went through a handoff and still did not finish is
            # reported as escalated, not as a plain failure: a person is
            # already involved and the distinction matters to the caller.
            result.status = ReplayStatus.ESCALATED

    def _extract(
        self, spec: CapabilityArtifact, obs: Observation, result: ReplayResult
    ) -> dict[str, Any]:
        outputs: dict[str, Any] = {}
        for out in spec.outputs:
            res = resolve(out.extract.control, obs)
            if not res.ok or res.node is None:
                if out.required:
                    result.drift_notes.append(f"output {out.name} could not be read")
                outputs[out.name] = None
                continue
            if res.used_fallback:
                result.drift_detected = True
            raw = _read(res.node, out.extract)
            outputs[out.name] = _transform(raw, out.extract, out.type)
            self.evidence.event(
                "extract", output=out.name,
                # The value itself is scrubbed by the writer if it is sensitive.
                value=outputs[out.name], via=res.strategy.kind.value if res.strategy else None)
        return outputs

    def _escalate(
        self, spec: CapabilityArtifact, result: ReplayResult, stop: _Stop
    ) -> InterventionRequest | None:
        obs = self.surface.perceive(1001)
        failure = stop.failure
        trigger = (InterventionTrigger.RISK_APPROVAL
                   if stop.status is ReplayStatus.ESCALATED
                   else InterventionTrigger.REPLAY_HARD_FAILURE)
        request = InterventionRequest(
            trigger=trigger,
            run_id=result.run_id,
            session_id=self.session_id or result.run_id,
            capability=spec.ref,
            tenant=spec.target.tenant,
            goal=spec.description,
            step_index=failure.step_index if failure else None,
            step_intent=failure.step_intent if failure else None,
            reason=(failure.observed if failure else "replay stopped"),
            url=obs.url,
            observation_summary=obs.render(max_texts=12),
            screenshot=self.surface.screenshot(self.evidence.screen_path("escalation")),
            requested_of_operator=(
                f"Replay of {spec.ref} stopped at step "
                f"{failure.step_index if failure else '?'}. Take control, complete or "
                f"abandon the task, then hand control back."),
        )
        self.evidence.event("escalated", intervention=request.id, trigger=trigger.value,
                            reason=request.reason)
        return self.broker.raise_intervention(request)

    # -- helpers ------------------------------------------------------------

    def _value_for(
        self, ref: ValueRef | None, args: dict[str, Any], params: list[ParamSpec]
    ) -> tuple[str | None, bool]:
        if ref is None:
            return None, False
        if ref.source == "literal":
            return ref.literal, False
        if ref.source == "param":
            spec = next((p for p in params if p.name == ref.ref), None)
            sensitive = bool(spec and spec.sensitivity in (Sensitivity.PII, Sensitivity.SECRET))
            return str(args.get(ref.ref, "")), sensitive
        if ref.source == "secret":
            env = ref.ref or ""
            value = os.environ.get(env) or os.environ.get(f"LEDGERHAND_{env}")
            if value is None:
                raise ArgumentError(f"secret {env!r} is not present in the environment")
            self.redactor.register(value, "secret")
            return value, True
        return None, False

    def _failure(
        self, step: Step, *, expected: str, observed: str, obs: Observation,
        attempts: list[str] | None = None,
    ) -> FailureDetail:
        """Capture the richer signal on the way out -- once, where it happens."""
        label = f"step_{step.index:02d}_failure"
        return FailureDetail(
            step_index=step.index, step_intent=step.intent,
            expected=expected, observed=observed,
            locator_attempts=attempts or [],
            url=obs.url,
            screenshot=self.surface.screenshot(self.evidence.screen_path(label)),
            dom_snapshot=self.surface.snapshot(self.evidence.snapshot_path(label)),
        )


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------

def _template(text: str, args: dict[str, Any]) -> str:
    return _TEMPLATE.sub(lambda m: str(args.get(m.group(1), m.group(0))), text or "")


def _coerce(param: ParamSpec, value: Any) -> Any:
    raw = str(value).strip()
    if param.type in (ParamType.INTEGER,):
        if not re.fullmatch(r"-?\d+", raw):
            raise ArgumentError(f"{param.name} must be an integer, got {value!r}")
        out: Any = int(raw)
    elif param.type in (ParamType.NUMBER, ParamType.MONEY):
        try:
            out = float(raw.replace(",", "").lstrip("$"))
        except ValueError:
            raise ArgumentError(f"{param.name} must be a number, got {value!r}") from None
    elif param.type is ParamType.BOOLEAN:
        out = raw.lower() in ("1", "true", "yes", "y")
    else:
        out = raw
    if param.pattern and not re.fullmatch(param.pattern, str(out)):
        raise ArgumentError(f"{param.name}={value!r} does not match {param.pattern}")
    if param.enum and str(out) not in param.enum:
        raise ArgumentError(f"{param.name} must be one of {param.enum}, got {value!r}")
    if param.minimum is not None and float(out) < param.minimum:
        raise ArgumentError(f"{param.name} must be >= {param.minimum}")
    if param.maximum is not None and float(out) > param.maximum:
        raise ArgumentError(f"{param.name} must be <= {param.maximum}")
    return out


def _read(node: UINode, spec: ExtractionSpec) -> str:
    if spec.attribute == "value":
        return node.value or node.text
    if spec.attribute == "attr":
        return node.attrs.get(spec.attr_name or "", "")
    return node.text or node.value


def _transform(raw: str, spec: ExtractionSpec, ptype: ParamType) -> Any:
    text = (raw or "").strip()
    if spec.transform == "regex" and spec.regex:
        match = re.search(spec.regex, text)
        text = (match.group(spec.group) if match else "").strip()
    if spec.transform == "money" or ptype is ParamType.MONEY:
        cleaned = text.replace(",", "").replace("$", "").strip()
        try:
            return float(cleaned)
        except ValueError:
            return None
    if spec.transform == "integer" or ptype is ParamType.INTEGER:
        digits = re.sub(r"[^\d-]", "", text)
        return int(digits) if digits else None
    return text


def _fingerprint(obs: Observation) -> str:
    """Must stay identical to the recorder's definition, or every replay
    reports drift against its own recording."""
    from ..agent.recorder import surface_fingerprint
    return surface_fingerprint(obs)


def new_run_id(prefix: str = "replay") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"
