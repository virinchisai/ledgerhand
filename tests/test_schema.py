"""The artifact contract: typing, versioning, tenant overlays, tool publication."""
from __future__ import annotations

import pathlib

import pytest

from ledgerhand.models.artifact import (
    CapabilityArtifact, ExtractionSpec, OutcomeSpec, OutputSpec, ParamSpec, Provenance,
    RecoverySpec, Step, TargetBinding, TenantOverlay, ValueRef,
)
from ledgerhand.models.conditions import text_present
from ledgerhand.models.enums import (
    ActionKind, LocatorKind, OutcomeClass, ParamType, RiskTier, Sensitivity,
)
from ledgerhand.models.locator import ControlDescriptor, LocatorStrategy, NameMatch

ROOT = pathlib.Path(__file__).resolve().parents[1]


def control(label: str, role: str = "textbox") -> ControlDescriptor:
    return ControlDescriptor(description=f"{role} {label!r}", strategies=[
        LocatorStrategy(kind=LocatorKind.LABEL_TEXT, rank=0, role=role,
                        label=NameMatch(value=label))])


def base_artifact() -> CapabilityArtifact:
    return CapabilityArtifact(
        id="member.balance.lookup", name="Lookup", description="Read a balance.",
        target=TargetBinding(product="meridian-core", product_version="7.2.14",
                             tenant="firstvalley",
                             entry_url="http://127.0.0.1:8848/t/firstvalley/"),
        inputs=[ParamSpec(name="member_id", description="member number",
                          sensitivity=Sensitivity.PII, pattern=r"[0-9]{4,12}")],
        outputs=[OutputSpec(name="savings_balance", type=ParamType.MONEY,
                            description="savings balance",
                            extract=ExtractionSpec(control=control("Balance", "cell"),
                                                   transform="money"))],
        steps=[
            Step(index=0, intent="enter the member id", action=ActionKind.TYPE,
                 target=control("Member ID"),
                 value=ValueRef(source="param", ref="member_id")),
            Step(index=1, intent="run the search", action=ActionKind.CLICK,
                 target=control("Search", "button"), risk=RiskTier.ELEVATED,
                 checkpoint=text_present("Member Detail")),
        ],
        provenance=Provenance(discovery_run_id="run_1", model="test"),
    )


# -- values ------------------------------------------------------------------

def test_secret_and_param_refs_cannot_carry_a_value():
    with pytest.raises(ValueError):
        ValueRef(source="secret", literal="hunter2")
    with pytest.raises(ValueError):
        ValueRef(source="param")           # a name is mandatory
    with pytest.raises(ValueError):
        ValueRef(source="literal", literal="x", ref="y")


def test_recoverable_outcome_must_declare_a_recovery():
    """Otherwise 'recoverable' is a label with no behaviour behind it."""
    with pytest.raises(ValueError):
        OutcomeSpec(code="X", classification=OutcomeClass.RECOVERABLE,
                    detect=text_present("boom"))
    OutcomeSpec(code="X", classification=OutcomeClass.RECOVERABLE,
                detect=text_present("boom"), recovery=RecoverySpec())


# -- contract publication ----------------------------------------------------

def test_tool_schema_is_a_usable_function_definition():
    schema = base_artifact().tool_schema()
    assert schema["name"] == "member_balance_lookup"
    prop = schema["input_schema"]["properties"]["member_id"]
    assert prop["type"] == "string" and prop["pattern"] == r"[0-9]{4,12}"
    assert prop["x-sensitivity"] == "pii"


def test_money_output_is_published_as_a_number():
    a = base_artifact()
    assert a.output("savings_balance").json_schema()["type"] == "number"


# -- versioning and identity -------------------------------------------------

def test_fingerprint_tracks_the_flow_not_the_counters():
    a, b = base_artifact(), base_artifact()
    b.stability.replays = 99
    assert a.fingerprint() == b.fingerprint()
    b.steps[0].value = ValueRef(source="literal", literal="12345")
    assert a.fingerprint() != b.fingerprint()


def test_max_risk_is_derived_from_the_steps():
    a = base_artifact()
    assert a.max_step_risk is RiskTier.ELEVATED
    a.steps[1].risk = RiskTier.IRREVERSIBLE
    assert a.max_step_risk is RiskTier.IRREVERSIBLE


# -- multi-tenant ------------------------------------------------------------

def test_overlay_specialises_one_capability_for_another_tenant():
    """Hundreds of tenants, one vendor product: an overlay is a reviewable diff,
    not a second recording that drifts away on its own."""
    a = base_artifact()
    a.overlays.append(TenantOverlay(
        tenant="summitcu", product_version="7.4.03",
        url_overrides={"entry_url": "http://127.0.0.1:8848/t/summitcu/"},
        control_overrides={"steps[0].target": control("Member Number")},
        notes="labels and route differ; flow is identical",
    ))
    resolved = a.resolve_for("summitcu")

    assert resolved.target.tenant == "summitcu"
    assert resolved.target.product_version == "7.4.03"
    assert resolved.target.entry_url.endswith("/t/summitcu/")
    assert "Member Number" in resolved.steps[0].target.description
    # The base is untouched -- resolve_for returns a specialisation.
    assert a.target.tenant == "firstvalley"
    assert "Member ID" in a.steps[0].target.description


def test_unknown_tenant_falls_through_to_the_base():
    a = base_artifact()
    assert a.resolve_for("someone-else") is a


def test_overlay_can_retarget_an_outputs_extraction():
    a = base_artifact()
    a.overlays.append(TenantOverlay(
        tenant="summitcu",
        control_overrides={"outputs[savings_balance].extract.control": control("Balance Due", "cell")}))
    resolved = a.resolve_for("summitcu")
    assert "Balance Due" in resolved.output("savings_balance").extract.control.description


def test_round_trips_through_json_without_loss():
    a = base_artifact()
    b = CapabilityArtifact.model_validate_json(a.model_dump_json())
    assert b.fingerprint() == a.fingerprint()
    assert b.steps[0].value.describe() == "@param:member_id"
