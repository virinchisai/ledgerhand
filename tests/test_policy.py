"""Guardrails: allowlist, risk classification, redaction."""
from __future__ import annotations

import pathlib

import pytest

from ledgerhand.models.enums import ActionKind, RiskTier
from ledgerhand.models.observation import UINode
from ledgerhand.safety.policy import PolicyGate, Verdict, load_profile
from ledgerhand.safety.redaction import Redactor

ROOT = pathlib.Path(__file__).resolve().parents[1]


@pytest.fixture
def gate():
    return PolicyGate(load_profile(ROOT / "policy.yaml"))


@pytest.mark.parametrize("url,allowed", [
    ("http://127.0.0.1:8848/t/firstvalley/", True),
    ("http://127.0.0.1:8848/t/summitcu/members/lookup", True),
    ("http://127.0.0.1:8848/admin/chaos", False),      # the app's own control plane
    ("http://127.0.0.1:8848/internal/export", False),  # outside the allowed prefix
    ("https://evil.example/t/firstvalley/", False),    # wrong origin
    ("", False),
])
def test_reach_is_default_deny(gate, url, allowed):
    assert gate.check_url(url).allowed is allowed


def test_agent_cannot_reach_the_targets_own_fault_injector(gate):
    """An agent able to inject faults into the app it is driving can manufacture
    the evidence that it succeeded."""
    decision = gate.check_url("http://127.0.0.1:8848/admin/chaos")
    assert not decision.allowed and "denied pattern" in decision.reason


@pytest.mark.parametrize("label,tier", [
    ("Open Account", RiskTier.IRREVERSIBLE),
    ("Transfer", RiskTier.IRREVERSIBLE),
    ("Search", RiskTier.ELEVATED),
    ("Acknowledge", RiskTier.ELEVATED),
    ("Back", RiskTier.SAFE),
])
def test_risk_is_classified_from_what_the_control_is_called(gate, label, tier):
    node = UINode(handle="e1", role="button", name=label)
    assert gate.classify_risk(ActionKind.CLICK, node) is tier


def test_reads_are_always_safe(gate):
    assert gate.classify_risk(ActionKind.EXTRACT, None) is RiskTier.SAFE


def test_irreversible_unattended_requires_confirmation(gate):
    assert gate.check_risk(RiskTier.IRREVERSIBLE, attended=False).verdict is Verdict.CONFIRM


def test_irreversible_attended_proceeds(gate):
    assert gate.check_risk(RiskTier.IRREVERSIBLE, attended=True).allowed


def test_readonly_profile_blocks_writes_outright():
    gate = PolicyGate(load_profile(ROOT / "policy.yaml", "readonly"))
    assert gate.check_risk(RiskTier.IRREVERSIBLE, attended=True).verdict is Verdict.DENY
    assert not gate.check_action(ActionKind.SELECT).allowed


def test_every_decision_is_recorded_for_evidence(gate):
    gate.check_url("https://evil.example/")
    gate.check_action(ActionKind.CLICK)
    assert len(gate.decisions) == 2
    assert len(gate.denials) == 1


# -- redaction --------------------------------------------------------------

def test_patterns_catch_regulated_identifiers():
    r = Redactor()
    scrubbed = r.scrub("ssn 123-45-6789 card 4111 1111 1111 1111 rtn 021000021")
    assert "123-45-6789" not in scrubbed and "4111" not in scrubbed
    assert "<redacted:ssn>" in scrubbed


def test_registered_literals_catch_what_patterns_cannot():
    """A password looks like ordinary text; only knowing the value stops it."""
    r = Redactor()
    r.register("Sandbox!Demo1", "secret")
    r.register("12345", "pii")
    assert r.scrub("signed in as svc with Sandbox!Demo1") == \
        "signed in as svc with <redacted:secret>"
    assert "12345" not in r.scrub("looked up member 12345")


def test_scrubbing_recurses_through_structures():
    r = Redactor()
    r.register("Sandbox!Demo1", "secret")
    out = r.scrub_obj({"a": ["pw=Sandbox!Demo1", {"b": "ssn 123-45-6789"}], "n": 7})
    assert out == {"a": ["pw=<redacted:secret>", {"b": "ssn <redacted:ssn>"}], "n": 7}


def test_longer_literals_win_over_shorter_overlapping_ones():
    r = Redactor()
    r.register("123", "pii")
    r.register("12345", "pii")
    assert r.scrub("12345") == "<redacted:pii>"
