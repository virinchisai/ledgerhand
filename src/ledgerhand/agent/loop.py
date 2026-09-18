"""The discovery loop: observe -> decide -> act, against a live surface.

This is the only place a model is ever in the decision path. Everything it
produces is treated as a *proposal*: validated against a closed schema, checked
against policy, and resolved against controls that were actually observed this
turn. A decision that names a handle which does not exist, an action the
profile forbids, or a value it was not given simply does not execute.

Stopping is as much of the design as stepping. A loop that can only end by
succeeding will burn its budget flailing; this one ends on success, on budget,
on the model conceding, and -- the interesting one -- on *lack of progress*,
which is what "stuck" actually looks like from outside.
"""
from __future__ import annotations

import hashlib
import os
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..evidence.recorder import EvidenceWriter
from ..models.artifact import ParamSpec, ValueRef
from ..models.enums import ActionKind, ParamType, RiskTier, Sensitivity
from ..models.intervention import InterventionRequest, InterventionTrigger
from ..models.observation import Observation, UINode
from ..safety.policy import PolicyGate, Verdict
from ..safety.redaction import Redactor
from ..surface.base import ActRequest, ActResult, Surface
from .llm import LLMClient, LLMReply
from .prompts import (
    GOAL_CHECK_SYSTEM, AgentDecision, applicable_actions, build_goal_check,
    build_system, build_user_prompt, render_readable, worth_goal_check,
)

#: Roles a person acts on, as opposed to reads. Mirrors prompts._is_actionable.
_ACTIONABLE_ROLES = frozenset({"textbox", "button", "combobox", "listbox",
                               "checkbox", "radio", "link", "menuitem"})

_ACTION_MAP = {
    "click": ActionKind.CLICK,
    "type": ActionKind.TYPE,
    "select": ActionKind.SELECT,
    "press": ActionKind.PRESS,
}


@dataclass
class OutputRequest:
    """An output the caller wants the capability to return."""
    name: str
    description: str
    type: ParamType = ParamType.STRING
    sensitivity: Sensitivity = Sensitivity.INTERNAL


@dataclass
class GoalSpec:
    """The discovery request: what to achieve, where, and under what contract.

    Note that param *values* live here but are never rendered into a prompt.
    The model is told a parameter exists and what it means, and emits
    `@param:name`; the executor substitutes. So the model drives a real member
    lookup without ever being shown a member number -- which is both a data
    handling win and the reason parameterisation is exact rather than inferred
    by matching strings back out of the recorded steps.
    """
    goal: str
    entry_url: str
    capability_id: str
    capability_name: str
    tenant: str
    product: str = "meridian-core"
    product_version: str = "unknown"
    params: list[ParamSpec] = field(default_factory=list)
    param_values: dict[str, str] = field(default_factory=dict)
    #: logical secret name -> environment variable holding it
    secret_env: dict[str, str] = field(default_factory=dict)
    #: What each secret IS. Without this the model is handed bare names like
    #: MCB_OPERATOR and has to guess which field they belong in -- and on the
    #: first real run it guessed wrong, typing the member id into the operator
    #: field. A reference is only useful if its meaning travels with it.
    secret_descriptions: dict[str, str] = field(default_factory=dict)
    outputs: list[OutputRequest] = field(default_factory=list)
    description: str = ""


@dataclass
class TraceStep:
    """One turn of the loop, kept in full for evidence and for the recorder."""
    index: int
    decision: AgentDecision | None
    action: ActionKind | None
    node: UINode | None
    value_ref: ValueRef | None
    result: ActResult | None
    observation: Observation
    url_before: str
    url_after: str
    risk: RiskTier = RiskTier.SAFE
    llm_latency_ms: int = 0
    llm_tokens: tuple[int, int] = (0, 0)
    screenshot: str | None = None
    note: str = ""

    @property
    def ok(self) -> bool:
        return bool(self.result and self.result.ok)


@dataclass
class DiscoveryResult:
    run_id: str
    status: str                       # succeeded | stuck | gave_up | denied | budget
    goal: GoalSpec
    trace: list[TraceStep] = field(default_factory=list)
    final_observation: Observation | None = None
    #: output name -> the node the model bound it to
    output_bindings: dict[str, UINode] = field(default_factory=dict)
    intervention: InterventionRequest | None = None
    reason: str = ""
    model: str = ""
    duration_s: float = 0.0

    @property
    def succeeded(self) -> bool:
        return self.status == "succeeded"


def _screen_hash(obs: Observation) -> str:
    """Identity of a screen *and how far through it we are*, for progress detection.

    Coarse on content: the URL plus the set of control labels, so a page whose
    only change is a spinner or a timestamp is not mistaken for progress.

    But it also records which controls now hold a value, and that part is
    load-bearing. Without it, filling a multi-field form looks identical to
    being stuck: select a type, type a nickname, type an amount, and the screen
    has "not changed" four times running -- which is exactly how the loop
    concluded it was stuck one click away from finishing. Filling in a field is
    progress. Retyping the same value into the same field is not, and still
    reads as no change.
    """
    controls = sorted(f"{n.role}:{n.label}:{n.value}" for n in obs.controls)
    return hashlib.sha256((obs.url + "|" + "|".join(controls)).encode()).hexdigest()[:16]


def _screen_identity(obs: Observation) -> str:
    """Which screen this is, ignoring what has been typed into it.

    Distinct from _screen_hash on purpose. Progress detection must notice a
    field being filled; the goal check must not -- "are the wanted values on
    this screen" depends on the screen, not on its contents, so a form being
    filled in is the *same* screen and must not be re-asked about.

    Keeping these separate is worth real time. Re-asking cost ~130s per form
    field, and worse, interleaving the two prompt shapes destroys the model's
    cached prefix: consecutive navigation calls reuse it and run at ~150s,
    while alternating nav/check pushed the same calls to ~520s. On CPU
    inference, prompt *order* is a performance decision.
    """
    controls = sorted(f"{n.role}:{n.label}" for n in obs.controls)
    return hashlib.sha256((obs.url + "|" + "|".join(controls)).encode()).hexdigest()[:16]


class DiscoveryAgent:
    """Runs one goal to completion, or to a documented stop."""

    def __init__(
        self,
        surface: Surface,
        llm: LLMClient,
        gate: PolicyGate,
        evidence: EvidenceWriter,
        *,
        redactor: Redactor | None = None,
        attended: bool = True,
        max_invalid: int = 3,
        no_progress_limit: int = 3,
    ) -> None:
        self.surface = surface
        self.llm = llm
        self.gate = gate
        self.evidence = evidence
        self.redactor = redactor or evidence.redactor
        #: Discovery is an attended activity by definition -- a person asked for
        #: it and is watching. Replay is not, which is why the same gate answers
        #: differently for the two paths.
        self.attended = attended
        self.max_invalid = max_invalid
        self.no_progress_limit = no_progress_limit

    # -- value references ---------------------------------------------------

    def _resolve_value(self, goal: GoalSpec, raw: str | None) -> tuple[str, ValueRef, bool]:
        """Turn a model-emitted reference into a concrete value + a recordable ref."""
        raw = (raw or "").strip()
        if raw.startswith(("@param:", "@secret:")):
            prefix, _, name = raw.partition(":")
            name = name.strip()
            # Accept the reference in either namespace. A small model mixes the
            # two prefixes up; it has still named the right thing, and the name
            # itself is unambiguous because parameters and secrets share no
            # names. Refusing here would burn a two-minute step to punish a
            # typo -- while a name in *neither* namespace is still refused,
            # because that would be the model inventing one.
            if name in goal.param_values:
                spec = next((p for p in goal.params if p.name == name), None)
                sensitive = bool(spec and spec.sensitivity in (Sensitivity.PII, Sensitivity.SECRET))
                return goal.param_values[name], ValueRef(source="param", ref=name), sensitive
            if name in goal.secret_env:
                env = goal.secret_env[name]
                value = os.environ.get(env)
                if value is None:
                    raise KeyError(f"secret {name!r} expects environment variable {env}")
                return value, ValueRef(source="secret", ref=name), True
            raise KeyError(
                f"{prefix[1:]} {name!r} is not declared; "
                f"available: {sorted(goal.param_values) + sorted(goal.secret_env)}")
        # A literal that happens to equal a parameter is recorded as the
        # parameter: otherwise the capability would be hard-wired to this run.
        for pname, pvalue in goal.param_values.items():
            if raw and raw == pvalue:
                return raw, ValueRef(source="param", ref=pname), False
        return raw, ValueRef(source="literal", literal=raw), False

    # -- the loop -----------------------------------------------------------

    def run(self, goal: GoalSpec, *, max_steps: int = 20) -> DiscoveryResult:
        run_id = self.evidence.run_id
        started = time.monotonic()
        budget = min(max_steps, self.gate.profile.max_steps)
        result = DiscoveryResult(run_id=run_id, status="budget", goal=goal, model=self.llm.name)

        # Register sensitive values so they cannot appear in any evidence.
        for spec in goal.params:
            if spec.sensitivity in (Sensitivity.PII, Sensitivity.SECRET):
                self.redactor.register(goal.param_values.get(spec.name), spec.sensitivity.value)
        for env in goal.secret_env.values():
            self.redactor.register(os.environ.get(env), "secret")

        self.evidence.event("run_start", goal=goal.goal, entry=goal.entry_url,
                            tenant=goal.tenant, model=self.llm.name, budget=budget,
                            policy_profile=self.gate.profile.name)

        # -- entry, through the same gate everything else uses --
        decision = self.gate.check_url(goal.entry_url)
        if not decision.allowed:
            result.status, result.reason = "denied", decision.reason
            self.evidence.event("policy_denied", scope="url", reason=decision.reason,
                                rule=decision.rule, url=goal.entry_url)
            return result
        self.surface.navigate(goal.entry_url)
        self.surface.settle()

        history: list[str] = []
        invalid_streak = 0
        seen_screens: list[str] = []
        checked_screens: set[str] = set()

        for step in range(1, budget + 1):
            if time.monotonic() - started > self.gate.profile.max_run_seconds:
                result.status, result.reason = "budget", "run time budget exhausted"
                break

            obs = self.surface.perceive(step)
            shot = self.surface.screenshot(self.evidence.screen_path(f"discover_{step:02d}"))
            result.final_observation = obs

            # -- progress check: the honest definition of stuck --
            # Only steps where we actually acted count. A model that timed out
            # or produced garbage left the screen unchanged by definition, and
            # charging that to the progress detector reports "the app is not
            # responding" when the truth is "the model is not responding".
            # Those are tracked separately by invalid_streak.
            window = seen_screens[-self.no_progress_limit - 1:]
            if len(seen_screens) > self.no_progress_limit and len(set(window)) == 1:
                result.status = "stuck"
                result.reason = (f"no state change across {self.no_progress_limit + 1} consecutive "
                                 f"steps on {obs.url}")
                break
            # Oscillation is not progress. Flipping a dropdown between two
            # values changes the screen every time, so a naive "did anything
            # change" test reads it as forward motion and the loop will happily
            # cycle until its budget runs out. Revisiting states already seen,
            # with no new one among them, is the same dead end wearing a hat.
            if len(seen_screens) > self.no_progress_limit * 2:
                recent = seen_screens[-self.no_progress_limit * 2:]
                if len(set(recent)) <= 2 and len(set(recent)) < len(recent):
                    result.status = "stuck"
                    result.reason = (f"cycling between {len(set(recent))} states over "
                                     f"{len(recent)} steps on {obs.url}")
                    break

            view = self._view(obs)

            # Ask the easy question first: are we already there?
            bindings = self._goal_check(goal, view, asked=checked_screens)
            if bindings is not None:
                live = {name: obs.node(node.handle) or node for name, node in bindings.items()}
                result.output_bindings = live
                result.status = "succeeded"
                result.reason = "goal state reached; all declared outputs located"
                result.trace.append(TraceStep(step, None, ActionKind.FINISH, None, None,
                                              ActResult(True, "goal reached"), obs,
                                              obs.url, obs.url, screenshot=shot))
                self.evidence.event("goal_reached", step=step,
                                    outputs={k: v.handle for k, v in live.items()})
                break

            reply = self._ask(goal, view, history)
            self.evidence.event("model_call", step=step, latency_ms=reply.latency_ms,
                                prompt_tokens=reply.prompt_tokens,
                                output_tokens=reply.output_tokens,
                                raw=reply.raw[:600], error=reply.error)

            decision_obj, problem = self._parse(reply)
            if decision_obj is None:
                invalid_streak += 1
                history.append(f"{step} (invalid model output: {problem})")
                self.evidence.event("model_invalid", step=step, problem=problem)
                if invalid_streak > self.max_invalid:
                    result.status = "stuck"
                    result.reason = f"model produced unusable output {invalid_streak}x: {problem}"
                    break
                continue
            invalid_streak = 0

            if decision_obj.action == "give_up":
                result.status, result.reason = "gave_up", decision_obj.reason or "model gave up"
                result.trace.append(TraceStep(step, decision_obj, None, None, None, None, obs,
                                              obs.url, obs.url, screenshot=shot))
                break

            if decision_obj.action == "finish":
                bindings, missing = self._bind_outputs(goal, obs, decision_obj)
                if missing:
                    history.append(f"{step} finish rejected: no value bound for {', '.join(missing)}")
                    self.evidence.event("finish_rejected", step=step, missing=missing)
                    invalid_streak += 1
                    if invalid_streak > self.max_invalid:
                        result.status = "stuck"
                        result.reason = f"model could not bind outputs: {', '.join(missing)}"
                        break
                    continue
                result.output_bindings = bindings
                result.status = "succeeded"
                result.reason = decision_obj.reason
                result.trace.append(TraceStep(step, decision_obj, ActionKind.FINISH, None, None,
                                              ActResult(True, "goal reached"), obs,
                                              obs.url, obs.url, screenshot=shot))
                self.evidence.event("goal_reached", step=step,
                                    outputs={k: v.handle for k, v in bindings.items()})
                break

            seen_screens.append(_screen_hash(obs))
            trace_step, fatal = self._execute(goal, obs, decision_obj, step, shot)
            result.trace.append(trace_step)
            history.append(self._history_line(step, trace_step))
            if fatal:
                result.status, result.reason = fatal
                break
            self.surface.settle()
        else:
            result.reason = result.reason or "step budget exhausted"

        result.duration_s = time.monotonic() - started
        self.evidence.event("run_end", status=result.status, reason=result.reason,
                            steps=len(result.trace), duration_s=round(result.duration_s, 1))
        return result

    # -- pieces -------------------------------------------------------------

    def _view(self, obs: Observation) -> Observation:
        """Build the view the model gets. Two different redaction rules apply.

        *Always*: values this system itself injected -- resolved secrets and PII
        parameters -- are scrubbed out. The model handed us a reference
        precisely so it would not hold the value; echoing the value back as the
        field's current contents would hand it over anyway, one step later. It
        still sees that the field is *filled*, which is the part it needs.

        *By policy*: pattern-based redaction of content the application itself
        displays. That is off by default here because the model is local and
        nothing leaves the host; point this at a hosted provider and it should
        be on.
        """
        view = obs.model_copy(deep=True)
        broad = self.gate.profile.redaction.redact_before_model
        for node in view.controls:
            node.value = self.redactor.scrub(node.value)
            if broad:
                node.text = self.redactor.scrub(node.text)
                node.attrs = {k: self.redactor.scrub(v) for k, v in node.attrs.items()}
        if broad:
            view.texts = [self.redactor.scrub(t) for t in view.texts]
            # URLs carry data too: `/members/detail/12345` puts the member id
            # in the address bar, which is exactly how identifiers leak into
            # places nobody thought to look.
            view.url = self.redactor.scrub(view.url)
            view.content_url = self.redactor.scrub(view.content_url)
        return view

    def _goal_check(
        self, goal: GoalSpec, view: Observation, *, asked: set[str] | None = None
    ) -> dict[str, UINode] | None:
        """Ask only "are the wanted values on this screen, and where?".

        This is the half of the problem the navigation question was smothering.
        Asked on its own it is a reading-comprehension task over one screen --
        roughly a fifth of the prompt, and a question a 7B model answers
        reliably. Fused into "what should I do next?", the same model walks off
        the screen holding the answer.

        Gated on the screen actually showing data, because on CPU inference an
        extra call costs minutes, and on a sign-on form the answer is certainly
        "no".
        """
        if not goal.outputs or not worth_goal_check(view):
            return None
        # Do not re-ask about a screen already ruled out. On CPU inference one
        # goal check costs minutes, and the answer for an unchanged screen
        # cannot have changed either.
        digest = _screen_identity(view)
        if asked is not None and digest in asked:
            return None
        reply = self.llm.decide(
            GOAL_CHECK_SYSTEM,
            build_goal_check(goal.goal,
                             [(o.name, o.description) for o in goal.outputs],
                             render_readable(view)),
            max_tokens=90)
        self.evidence.event("goal_check", step=view.step, latency_ms=reply.latency_ms,
                            prompt_tokens=reply.prompt_tokens, raw=reply.raw[:300],
                            error=reply.error)
        if not reply.data:
            return None
        # Where the readable content actually lives. A value bound to the
        # navigation chrome is a miss dressed up as a hit -- and because a
        # partial hit is retried rather than ruled out, one bogus binding makes
        # the check re-fire on every later step.
        frames = [tuple(n.frame_path) for n in view.controls
                  if n.text and n.role not in _ACTIONABLE_ROLES]
        content_frame = max(set(frames), key=frames.count) if frames else ()

        bindings: dict[str, UINode] = {}
        for want in goal.outputs:
            handle = str(reply.data.get(want.name, "none")).strip().strip("[]").strip()
            node = view.node(handle) if handle and handle.lower() != "none" else None
            if node is None or not (node.text or node.value):
                continue
            if frames and tuple(node.frame_path) != content_frame:
                continue
            bindings[want.name] = node
        if len(bindings) == len(goal.outputs):
            return bindings
        # Only rule the screen out when *nothing* matched. A partial hit means
        # we are on the right screen and the model missed one value -- locking
        # it out would guarantee the run never finishes, having already done
        # the irreversible thing it came to do.
        if asked is not None and not bindings:
            asked.add(digest)
        return None

    def _ask(self, goal: GoalSpec, view: Observation, history: list[str]) -> LLMReply:
        """The navigation question: one action, no output binding."""
        user = build_user_prompt(
            goal=goal.goal, obs=view,
            params=[(p.name, p.description) for p in goal.params],
            secrets=[(name, goal.secret_descriptions.get(name, ""))
                     for name in goal.secret_env],
            outputs=[(o.name, o.description) for o in goal.outputs],
            history=history,
            include_readable=False,
        )
        return self.llm.decide(
            build_system(applicable_actions(view, include_finish=False)), user)

    @staticmethod
    def _parse(reply: LLMReply) -> tuple[AgentDecision | None, str]:
        if reply.error:
            return None, reply.error
        if not reply.data:
            return None, "no JSON object in response"
        try:
            return AgentDecision.model_validate(reply.data), ""
        except Exception as exc:
            return None, str(exc).splitlines()[0]

    @staticmethod
    def _bind_outputs(
        goal: GoalSpec, obs: Observation, decision: AgentDecision
    ) -> tuple[dict[str, UINode], list[str]]:
        """A finish is only accepted if every declared output resolves to a node
        that is actually on screen right now."""
        bindings: dict[str, UINode] = {}
        missing: list[str] = []
        for want in goal.outputs:
            handle = decision.outputs.get(want.name)
            node = obs.node(handle) if handle else None
            if node is None or not (node.text or node.value):
                missing.append(want.name)
            else:
                bindings[want.name] = node
        return bindings, missing

    def _execute(
        self, goal: GoalSpec, obs: Observation, decision: AgentDecision,
        step: int, shot: str | None,
    ) -> tuple[TraceStep, tuple[str, str] | None]:
        kind = _ACTION_MAP[decision.action]
        node = obs.node(decision.target) if decision.target else None
        url_before = obs.url

        def fail(note: str) -> tuple[TraceStep, None]:
            self.evidence.event("action_rejected", step=step, reason=note,
                                decision=decision.describe())
            return TraceStep(step, decision, kind, node, None,
                             ActResult(False, error=note), obs, url_before, url_before,
                             screenshot=shot, note=note), None

        if kind is not ActionKind.PRESS and node is None:
            return fail(f"handle {decision.target!r} is not on screen")

        verdict = self.gate.check_action(kind)
        if not verdict.allowed:
            return fail(f"policy: {verdict.reason}")

        risk = self.gate.classify_risk(kind, node)
        risk_decision = self.gate.check_risk(risk, attended=self.attended)
        if risk_decision.verdict is Verdict.DENY:
            self.evidence.event("policy_denied", scope="risk", step=step,
                                reason=risk_decision.reason, risk=risk.value,
                                control=node.label if node else "")
            return (TraceStep(step, decision, kind, node, None,
                              ActResult(False, error=risk_decision.reason), obs,
                              url_before, url_before, risk=risk, screenshot=shot,
                              note=risk_decision.reason),
                    ("denied", risk_decision.reason))
        if risk_decision.verdict is Verdict.CONFIRM:
            # Unattended discovery never commits an irreversible act on its own.
            self.evidence.event("confirmation_required", step=step, risk=risk.value,
                                control=node.label if node else "")
            return (TraceStep(step, decision, kind, node, None,
                              ActResult(False, error="human confirmation required"), obs,
                              url_before, url_before, risk=risk, screenshot=shot,
                              note="confirmation required"),
                    ("stuck", f"irreversible action on {node.label if node else '?'} "
                              f"needs human confirmation"))

        try:
            value, ref, sensitive = self._resolve_value(goal, decision.value)
        except KeyError as exc:
            return fail(str(exc))

        request = ActRequest(kind=kind, node=node, value=value,
                             key=decision.key, sensitive=sensitive)
        outcome = self.surface.act(request)
        self.evidence.event(
            "action", step=step, action=kind.value,
            control=(node.label if node else decision.key),
            value_ref=ref.describe() if ref else None,
            ok=outcome.ok, detail=outcome.detail, error=outcome.error,
            risk=risk.value, reason=decision.reason[:160],
        )
        return TraceStep(step, decision, kind, node, ref, outcome, obs,
                         url_before, self.surface.current_url(), risk=risk,
                         screenshot=shot), None

    @staticmethod
    def _history_line(step: int, ts: TraceStep) -> str:
        what = ts.decision.describe() if ts.decision else "?"
        if ts.value_ref and ts.value_ref.source != "literal":
            what = f"{ts.decision.action} {ts.node.label if ts.node else ''} = {ts.value_ref.describe()}"
        status = "ok" if ts.ok else f"FAILED ({ts.result.error if ts.result else 'n/a'})"
        return f"{step} {what} -> {status}"


def build_intervention(result: DiscoveryResult, session_id: str) -> InterventionRequest:
    """Package a stuck discovery run for a human.

    The packet has to be actionable without the conversation that produced it:
    what was wanted, how far we got, what is on screen, and what the operator is
    being asked to do.
    """
    last = result.trace[-1] if result.trace else None
    obs = result.final_observation
    trigger = {
        "stuck": InterventionTrigger.AGENT_STUCK,
        "gave_up": InterventionTrigger.AGENT_STUCK,
        "budget": InterventionTrigger.AGENT_STUCK,
        "denied": InterventionTrigger.RISK_APPROVAL,
    }.get(result.status, InterventionTrigger.UNKNOWN_STATE)
    return InterventionRequest(
        trigger=trigger,
        run_id=result.run_id,
        session_id=session_id,
        goal=result.goal.goal,
        tenant=result.goal.tenant,
        capability=result.goal.capability_id,
        step_index=last.index if last else None,
        step_intent=(last.decision.reason if last and last.decision else None),
        reason=result.reason,
        url=obs.url if obs else "",
        observation_summary=(obs.render(max_texts=10) if obs else ""),
        screenshot=last.screenshot if last else None,
        requested_of_operator=(
            f"Take control and complete: {result.goal.goal}. "
            f"Then hand control back so the run can record the flow."
        ),
    )
