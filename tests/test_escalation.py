"""Human-in-the-loop: detecting stuck, transferring control, resuming.

These drive the real broker against a real browser session -- the same objects
and the same calls the HTML operator console posts to. Only the person is
simulated.
"""
from __future__ import annotations

import pathlib

import pytest

from ledgerhand.agent.loop import DiscoveryAgent, build_intervention
from ledgerhand.escalation.broker import ControlError, LeasedSurface, SessionBroker
from ledgerhand.escalation.console import AutoOperator, build_console
from ledgerhand.evidence.recorder import EvidenceWriter
from ledgerhand.goalspec import load_goal
from ledgerhand.models.enums import ActionKind, ControlOwner, ReplayStatus
from ledgerhand.models.intervention import InterventionStatus, InterventionTrigger
from ledgerhand.replay.engine import ReplayEngine
from ledgerhand.safety.policy import PolicyGate, load_profile
from ledgerhand.safety.redaction import Redactor
from ledgerhand.surface.base import ActRequest
from ledgerhand.surface.web import WebSurface
from tests.oracle import OracleClient
from tests.test_pipeline import BIND, PLAN

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def rig(app_server, tmp_path):
    surface = WebSurface(headless=True)
    surface.start()
    evidence = EvidenceWriter(tmp_path, "esc_run", Redactor())
    broker = SessionBroker(surface, evidence)
    leased = LeasedSurface(surface, broker, ControlOwner.AGENT)
    gate = PolicyGate(load_profile(ROOT / "policy.yaml"))
    yield surface, leased, broker, gate, evidence
    surface.close()


# -- detecting stuck ---------------------------------------------------------

def test_agent_that_cannot_proceed_raises_an_actionable_intervention(rig):
    surface, leased, broker, gate, evidence = rig
    goal, _ = load_goal(ROOT / "goals" / "member-savings-balance.yaml")
    # An oracle that can sign on but knows nothing about the search screen.
    stunted = OracleClient(plan=[p for p in PLAN if p[0] in ("Operator ID", "Password", "Sign On")])
    result = DiscoveryAgent(leased, stunted, gate, evidence, attended=True).run(goal, max_steps=8)
    assert not result.succeeded

    request = broker.raise_intervention(build_intervention(result, broker.session_id))
    assert request.trigger is InterventionTrigger.AGENT_STUCK
    # The packet must be actionable without the conversation that produced it.
    assert request.goal and request.url and request.observation_summary
    assert request.requested_of_operator
    assert request.session_id == broker.session_id


def test_no_progress_is_detected_before_the_step_budget_runs_out(rig):
    """Being stuck is 'the screen stopped changing', not 'we ran out of steps'.

    An agent that keeps acting without moving would otherwise burn its whole
    budget before anyone heard about it.
    """
    surface, leased, broker, gate, evidence = rig
    goal, _ = load_goal(ROOT / "goals" / "member-savings-balance.yaml")

    class Repeating(OracleClient):
        """Retypes the same field forever: every action succeeds, nothing moves."""

        def decide(self, system, user, *, max_tokens=110):
            self.done.clear()
            return super().decide(system, user, max_tokens=max_tokens)

    agent = DiscoveryAgent(
        leased,
        Repeating(plan=[("Operator ID", {"action": "type", "value": "@secret:MCB_OPERATOR"})]),
        gate, evidence, attended=True)
    result = agent.run(goal, max_steps=16)

    assert result.status == "stuck", result.reason
    assert "no state change" in result.reason
    assert len(result.trace) < 16, "should stop well before exhausting the budget"


# -- the control lease -------------------------------------------------------

def test_automation_cannot_act_while_a_human_holds_the_session(rig):
    surface, leased, broker, gate, evidence = rig
    surface.navigate("http://127.0.0.1:8848/t/firstvalley/")
    obs = surface.perceive(0)
    button = next(n for n in obs.controls if n.role == "button")

    assert leased.act(ActRequest(ActionKind.CLICK, node=button)).ok

    request = broker.raise_intervention(build_intervention(
        _stub_result(), broker.session_id))
    assert broker.lease.owner is ControlOwner.NONE
    with pytest.raises(ControlError):
        leased.act(ActRequest(ActionKind.CLICK, node=button))

    broker.claim(request.id, "op.jane")
    token = broker.grant_control(request.id)
    assert broker.lease.owner is ControlOwner.OPERATOR
    with pytest.raises(ControlError):
        leased.act(ActRequest(ActionKind.CLICK, node=button))

    broker.handback(request.id, token, resume=True, resolution="done")
    assert broker.lease.owner is ControlOwner.AGENT
    # Re-observe first: a node handle is only valid for the observation it came
    # from, which is exactly the guarantee we want after someone else has been
    # driving. Acting on a pre-handoff handle is not allowed to work.
    assert not leased.act(ActRequest(ActionKind.CLICK, node=button)).ok
    fresh = next(n for n in surface.perceive(0).controls if n.role == "button")
    assert leased.act(ActRequest(ActionKind.CLICK, node=fresh)).ok


def test_a_stale_token_cannot_act_after_handback(rig):
    """The failure this prevents: automation resuming while a person is still
    typing, or an operator acting after they released the session."""
    surface, leased, broker, gate, evidence = rig
    request = broker.raise_intervention(build_intervention(_stub_result(), broker.session_id))
    broker.claim(request.id, "op.jane")
    token = broker.grant_control(request.id)
    broker.handback(request.id, token, resume=True)
    with pytest.raises(ControlError):
        broker.assert_control(ControlOwner.OPERATOR, token)


# -- the handoff, end to end -------------------------------------------------

def test_operator_takes_the_live_session_acts_and_hands_back(rig):
    """The human drives the *same* session, on the page the automation left."""
    surface, leased, broker, gate, evidence = rig
    surface.navigate("http://127.0.0.1:8848/t/firstvalley/")
    surface.settle()
    request = broker.raise_intervention(build_intervention(_stub_result(), broker.session_id))
    broker.claim(request.id, "op.jane")
    token = broker.grant_control(request.id)

    obs = broker.operator_observe(token)
    operator_id = next(n for n in obs.controls if "Operator" in n.label)
    password = next(n for n in obs.controls if "Password" in n.label)
    sign_on = next(n for n in obs.controls if n.role == "button")

    broker.operator_act(request.id, token, kind=ActionKind.TYPE,
                        handle=operator_id.handle, value="svc.automation")
    broker.operator_act(request.id, token, kind=ActionKind.TYPE,
                        handle=password.handle, value="Sandbox!Demo1")
    broker.operator_act(request.id, token, kind=ActionKind.CLICK, handle=sign_on.handle)

    returned = broker.handback(request.id, token, resume=True,
                               resolution="signed on manually")
    assert returned.status is InterventionStatus.RETURNED
    assert returned.resume_requested
    # The human's actions are part of the run's evidence.
    kinds = [a.kind for a in returned.operator_actions]
    assert kinds.count("type") == 2 and "click" in kinds
    # And the session really moved -- automation resumes where they left it.
    assert "/console" in surface.current_url()


def test_operator_actions_are_recorded_without_leaking_the_password(rig):
    surface, leased, broker, gate, evidence = rig
    evidence.redactor.register("Sandbox!Demo1", "secret")
    surface.navigate("http://127.0.0.1:8848/t/firstvalley/")
    request = broker.raise_intervention(build_intervention(_stub_result(), broker.session_id))
    broker.claim(request.id, "op.jane")
    token = broker.grant_control(request.id)
    obs = broker.operator_observe(token)
    password = next(n for n in obs.controls if "Password" in n.label)
    broker.operator_act(request.id, token, kind=ActionKind.TYPE,
                        handle=password.handle, value="Sandbox!Demo1")
    log = pathlib.Path(evidence.log_path).read_text()
    assert "Sandbox!Demo1" not in log
    assert "<redacted:secret>" in log


def test_replay_parks_for_a_handoff_and_resumes_on_the_same_session(rig, chaos):
    """Pause, cede, resume -- the seam the brief asks for, exercised for real."""
    surface, leased, broker, gate, evidence = rig
    artifact = _record_artifact(surface, gate, evidence)

    engine = ReplayEngine(leased, gate, evidence, redactor=evidence.redactor,
                          attended=False, broker=broker, session_id=broker.session_id,
                          handoff_timeout_s=45.0)
    _watch_and_handle(broker, AutoOperator(broker))

    chaos(expire_session=True)     # a hard failure replay must not paper over
    result = engine.run(artifact, {"member_id": "12345"})

    assert result.intervention_id, "the hard failure should have raised an intervention"
    request = broker.interventions[result.intervention_id]
    assert request.status in (InterventionStatus.RETURNED, InterventionStatus.RESOLVED)
    assert request.operator == "auto.operator"
    assert broker.lease.owner is ControlOwner.AGENT, "control must come back"
    # The handoff is part of the run's own report, not a side channel.
    assert any(s.action == "handoff" for s in result.steps)
    assert result.status is ReplayStatus.ESCALATED


def test_console_renders_the_queue_and_the_detail(rig):
    from fastapi.testclient import TestClient
    surface, leased, broker, gate, evidence = rig
    request = broker.raise_intervention(build_intervention(_stub_result(), broker.session_id))
    client = TestClient(build_console(broker))
    assert request.id in client.get("/").text
    detail = client.get(f"/i/{request.id}")
    assert detail.status_code == 200
    assert "Why the automation stopped" in detail.text
    assert "Claim" in detail.text


# -- helpers -----------------------------------------------------------------

def _stub_result():
    from ledgerhand.agent.loop import DiscoveryResult, GoalSpec
    goal = GoalSpec(goal="demo goal", entry_url="http://127.0.0.1:8848/t/firstvalley/",
                    capability_id="demo.cap", capability_name="Demo", tenant="firstvalley")
    return DiscoveryResult(run_id="r1", status="stuck", goal=goal,
                           reason="no state change across 4 consecutive steps")


def _watch_and_handle(broker, operator, *, resolution="operator released the session"):
    """Stand in for a person watching the console."""
    import threading
    import time

    def watch():
        for _ in range(400):
            if broker.pending:
                operator.run_async(broker.pending[0].id, steps=[],
                                   resolution=resolution, resume=False)
                return
            time.sleep(0.05)

    threading.Thread(target=watch, daemon=True).start()


def _record_artifact(surface, gate, evidence):
    """Record a capability on an already-open surface.

    Playwright's sync API pins one Playwright instance per thread, so tests that
    need both a recording and a live session share one browser rather than
    starting a second.
    """
    import httpx
    from ledgerhand.agent.recorder import record
    from ledgerhand.products.profile import load_product

    httpx.post("http://127.0.0.1:8848/admin/reset", timeout=5.0)
    goal, codes = load_goal(ROOT / "goals" / "member-savings-balance.yaml")
    result = DiscoveryAgent(surface, OracleClient(plan=list(PLAN), bind=dict(BIND)), gate, evidence,
                            attended=True).run(goal, max_steps=12)
    assert result.succeeded, result.reason
    return record(result, load_product(ROOT / "products" / "meridian-core.yaml"))
