"""The end-to-end thread: discovery -> artifact -> deterministic replay.

Driven by a deterministic oracle rather than a live model, so it runs in CI and
covers the paths a real model would rarely take.
"""
from __future__ import annotations

import json
import pathlib

import pytest

from ledgerhand.agent.loop import DiscoveryAgent
from ledgerhand.agent.recorder import RecorderConfig, record
from ledgerhand.evidence.recorder import EvidenceWriter
from ledgerhand.goalspec import load_goal
from ledgerhand.models.enums import ParamType, ReplayStatus, RiskTier, Sensitivity
from ledgerhand.products.profile import load_product
from ledgerhand.replay.engine import ReplayEngine
from ledgerhand.safety.policy import PolicyGate, load_profile
from ledgerhand.safety.redaction import Redactor
from ledgerhand.surface.web import WebSurface
from tests.oracle import OracleClient

ROOT = pathlib.Path(__file__).resolve().parents[1]

#: The route through the app, expressed the way the oracle understands it.
PLAN = [
    ("Operator ID", {"action": "type", "value": "@secret:MCB_OPERATOR"}),
    ("Password", {"action": "type", "value": "@secret:MCB_PASSWORD"}),
    ("Sign On", {"action": "click", "role": "button"}),
    ("Member ID", {"action": "type", "value": "@param:member_id"}),
    ("Search", {"action": "click", "role": "button"}),
]

#: What the goal check should answer once the detail screen is up.
BIND = {"savings_balance": "4,182.55", "member_name": "DANA WHITFIELD"}


@pytest.fixture
def surface():
    s = WebSurface(headless=True)
    s.start()
    yield s
    s.close()


@pytest.fixture
def gate():
    return PolicyGate(load_profile(ROOT / "policy.yaml"))


@pytest.fixture
def product():
    return load_product(ROOT / "products" / "meridian-core.yaml")


@pytest.fixture
def evidence(tmp_path):
    return EvidenceWriter(tmp_path, "run_test", Redactor())


@pytest.fixture(scope="module")
def artifact(tmp_path_factory):
    """Discover once for the whole module, then replay it many ways.

    Module-scoped on purpose: discovery is the expensive half and every test
    below replays the *same* artifact, which is also a stronger statement --
    one recording has to serve every case, rather than each test getting a
    freshly-tailored one.
    """
    import httpx
    httpx.post("http://127.0.0.1:8848/admin/reset", timeout=5.0)
    goal, codes = load_goal(ROOT / "goals" / "member-savings-balance.yaml")
    surface = WebSurface(headless=True)
    surface.start()
    try:
        evidence = EvidenceWriter(tmp_path_factory.mktemp("discovery"), "run_record", Redactor())
        gate = PolicyGate(load_profile(ROOT / "policy.yaml"))
        product = load_product(ROOT / "products" / "meridian-core.yaml")
        agent = DiscoveryAgent(surface, OracleClient(plan=list(PLAN), bind=dict(BIND)), gate, evidence,
                               attended=True)
        result = agent.run(goal, max_steps=12)
        assert result.succeeded, f"discovery did not reach the goal: {result.status} {result.reason}"
        return record(result, product, config=RecorderConfig(outcome_codes=codes))
    finally:
        surface.close()


# ---------------------------------------------------------------------------
# the artifact
# ---------------------------------------------------------------------------

def test_artifact_captures_the_contract(artifact):
    assert artifact.id == "member.savings_balance.lookup"
    assert [p.name for p in artifact.inputs] == ["member_id"]
    assert {o.name for o in artifact.outputs} == {"savings_balance", "member_name"}
    assert artifact.success_condition is not None
    assert artifact.steps, "a recorded capability must have steps"
    assert all(s.checkpoint is not None for s in artifact.steps[-1:]), \
        "the final step must assert that the goal state was reached"


def test_artifact_never_contains_secret_or_pii_values(artifact):
    """The whole reason ValueRef carries a name instead of a value."""
    blob = json.dumps(artifact.model_dump(mode="json"))
    assert "Sandbox!Demo1" not in blob
    assert "svc.automation" not in blob
    assert "DANA WHITFIELD" not in blob
    assert '"12345"' not in blob, "the discovery run's member id leaked into the artifact"


def test_values_are_recorded_as_references(artifact):
    sources = {s.value.source for s in artifact.steps if s.value}
    assert "secret" in sources and "param" in sources
    assert "literal" not in sources or all(
        s.value.literal not in ("Sandbox!Demo1", "12345")
        for s in artifact.steps if s.value and s.value.source == "literal")


def test_money_output_is_typed_and_anchored(artifact):
    balance = artifact.output("savings_balance")
    assert balance.type is ParamType.MONEY
    assert balance.extract.transform == "money"
    primary = balance.extract.control.primary
    # Anchored on the account TYPE, which is stable, not the account number.
    assert "SAVINGS" in primary.describe(), primary.describe()


def test_pii_output_is_classified(artifact):
    assert artifact.output("member_name").sensitivity is Sensitivity.PII


def test_capability_publishes_as_a_tool_schema(artifact):
    schema = artifact.tool_schema()
    assert schema["name"] == "member_savings_balance_lookup"
    assert schema["input_schema"]["required"] == ["member_id"]
    assert schema["input_schema"]["properties"]["member_id"]["x-sensitivity"] == "pii"
    assert "MEMBER_NOT_FOUND" in schema["description"]


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------

def _engine(surface, gate, evidence, **kw):
    return ReplayEngine(surface, gate, evidence, redactor=evidence.redactor, **kw)


def test_replay_succeeds_and_returns_typed_outputs(artifact, surface, gate, evidence):
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
    assert result.status is ReplayStatus.SUCCESS, result.summary()
    assert result.outputs["savings_balance"] == 4182.55
    assert result.outputs["member_name"] == "DANA WHITFIELD"


def test_replay_generalises_to_a_different_member(artifact, surface, gate, evidence):
    """The capability was recorded against 12345; it must work for anyone."""
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "23456"})
    assert result.status is ReplayStatus.SUCCESS, result.summary()
    assert result.outputs["savings_balance"] == 27430.09
    assert result.outputs["member_name"] == "MARCUS OYELARAN"


def test_replay_is_deterministic_across_runs(artifact, surface, gate, evidence):
    runs = [_engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
            for _ in range(3)]
    assert {r.status for r in runs} == {ReplayStatus.SUCCESS}
    assert {r.outputs["savings_balance"] for r in runs} == {4182.55}
    resolved = {s.locator.resolved_by for r in runs for s in r.steps if s.locator}
    assert all(not s.locator.used_fallback for r in runs for s in r.steps if s.locator), \
        f"replay fell back to a lower-ranked locator: {resolved}"


# -- the taxonomy -----------------------------------------------------------

def test_unknown_member_is_a_business_outcome_not_a_failure(artifact, surface, gate, evidence):
    """The distinction the brief calls the most common design mistake."""
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "99999"})
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "MEMBER_NOT_FOUND"
    assert result.ok, "a legitimate 'not found' is a successful call"
    assert result.failure is None


def test_restricted_member_is_a_business_outcome(artifact, surface, gate, evidence):
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "55555"})
    assert result.status is ReplayStatus.BUSINESS_OUTCOME
    assert result.outcome_code == "ACCESS_DENIED"


def test_interstitial_is_recovered_and_the_run_completes(artifact, surface, gate, evidence, chaos):
    chaos(interstitial=True)
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
    assert result.status is ReplayStatus.SUCCESS, result.summary()
    assert any("MAINTENANCE_INTERSTITIAL" in r for step in result.steps
               for r in step.recoveries), "the interstitial should have been cleared"


def test_session_expiry_is_a_hard_failure_with_debuggable_detail(
        artifact, surface, gate, evidence, chaos):
    chaos(expire_session=True)
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
    assert result.status is ReplayStatus.FAILED
    assert result.outcome_code == "SESSION_EXPIRED"
    assert result.failure is not None
    assert result.failure.step_index >= 0
    assert result.failure.screenshot, "a hard failure must capture richer evidence"


def test_app_error_is_a_hard_failure(artifact, surface, gate, evidence, chaos):
    chaos(server_error=True)
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
    assert result.status is ReplayStatus.FAILED
    assert not result.ok


def test_slow_load_is_absorbed_without_failing(artifact, surface, gate, evidence, chaos):
    chaos(slow_ms=900)
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
    assert result.status is ReplayStatus.SUCCESS, result.summary()


# -- the input contract -----------------------------------------------------

def test_bad_arguments_are_rejected_before_touching_the_surface(
        artifact, surface, gate, evidence):
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "not-a-number"})
    assert result.status is ReplayStatus.FAILED
    assert "does not match" in result.failure.observed
    assert result.steps == [], "argument validation must happen before any action"


def test_missing_argument_is_rejected(artifact, surface, gate, evidence):
    result = _engine(surface, gate, evidence).run(artifact, {})
    assert "missing required argument" in result.failure.observed


def test_unknown_argument_is_rejected(artifact, surface, gate, evidence):
    result = _engine(surface, gate, evidence).run(artifact, {"member_id": "12345", "oops": "1"})
    assert "unknown argument" in result.failure.observed


# -- evidence ---------------------------------------------------------------

def test_evidence_contains_no_secrets(artifact, surface, gate, evidence):
    _engine(surface, gate, evidence).run(artifact, {"member_id": "12345"})
    log = pathlib.Path(evidence.log_path).read_text()
    assert "Sandbox!Demo1" not in log
    assert "12345" not in log, "the PII argument leaked into the run log"


# ---------------------------------------------------------------------------
# what the model is allowed to see
# ---------------------------------------------------------------------------

def test_the_model_is_never_handed_back_a_value_it_was_not_given(app_server, gate, evidence):
    """The reference protocol is only worth something if it holds both ways.

    The model emits `@secret:MCB_PASSWORD` instead of a password. If the next
    turn's screen then shows it the resolved value as the field's contents, it
    has the secret anyway -- one step later and with no audit trail.

    Note the boundary this test draws. Credentials are only ever on screen
    because *we* typed them, so they must never appear at all. A member id is
    different: the application prints it on the detail screen, and the model has
    to read that screen to operate. Redacting content the app itself displays is
    a separate, policy-controlled decision -- see the test below.
    """
    goal, _ = load_goal(ROOT / "goals" / "member-savings-balance.yaml")
    surface = WebSurface(headless=True)
    surface.start()
    try:
        oracle = OracleClient(plan=list(PLAN), bind=dict(BIND))
        result = DiscoveryAgent(surface, oracle, gate, evidence, attended=True).run(
            goal, max_steps=12)
        assert result.succeeded, result.reason
        everything = "\n".join(oracle.calls)

        assert "Sandbox!Demo1" not in everything, "the password reached the prompt"
        assert "svc.automation" not in everything, "the operator id reached the prompt"
        # The PII argument must never come back as a control's contents...
        assert "current='12345'" not in everything
        assert '"12345"' not in everything.replace('"12345") ', "")
        # ...while the model can still tell the field was filled.
        assert "<filled>" in everything
    finally:
        surface.close()


def test_hosted_profile_redacts_screen_content_before_the_model_sees_it(
        app_server, evidence):
    """With a hosted model, screen content leaves the host, so it is scrubbed."""
    goal, _ = load_goal(ROOT / "goals" / "member-savings-balance.yaml")
    hosted = PolicyGate(load_profile(ROOT / "policy.yaml", "hosted"))
    surface = WebSurface(headless=True)
    surface.start()
    try:
        oracle = OracleClient(plan=list(PLAN), bind=dict(BIND))
        DiscoveryAgent(surface, oracle, hosted, evidence, attended=True).run(
            goal, max_steps=12)
        everything = "\n".join(oracle.calls)
        assert "12345" not in everything, "the member id reached a hosted prompt"
        assert "Sandbox!Demo1" not in everything
    finally:
        surface.close()


def test_a_reference_in_the_wrong_namespace_is_still_honoured(app_server, gate, evidence):
    """`@param:MCB_OPERATOR` names a secret. The model got the namespace wrong
    and the *reference* right; refusing would cost a step to punish a typo.
    A name in neither namespace is still refused."""
    from ledgerhand.agent.loop import DiscoveryAgent as _Agent
    goal, _ = load_goal(ROOT / "goals" / "member-savings-balance.yaml")
    surface = WebSurface(headless=True)
    surface.start()
    try:
        agent = _Agent(surface, OracleClient(), gate, evidence, attended=True)
        value, ref, sensitive = agent._resolve_value(goal, "@param:MCB_OPERATOR")
        assert ref.source == "secret" and ref.ref == "MCB_OPERATOR" and sensitive
        value, ref, _ = agent._resolve_value(goal, "@secret:member_id")
        assert ref.source == "param" and ref.ref == "member_id"
        with pytest.raises(KeyError):
            agent._resolve_value(goal, "@param:not_a_thing")
    finally:
        surface.close()
