"""Loading product profiles, and the compact condition syntax they use.

The verbose Condition model is right for machines; nobody wants to hand-write
it. So profiles are authored in a compact form -- `{any: [{text: "..."}]}` --
and expanded here into the same typed objects the engine evaluates.
"""
from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Any

import yaml

from ..models.artifact import RecoverySpec, Step
from ..models.conditions import Condition
from ..models.enums import ActionKind, LocatorKind, OutcomeClass, RiskTier
from ..models.locator import ControlDescriptor, LocatorStrategy, NameMatch
from ..models.artifact import OutcomeSpec


def parse_condition(node: Any) -> Condition:
    """Expand the compact profile syntax into a Condition."""
    if node is None:
        raise ValueError("empty condition")
    if isinstance(node, str):
        return Condition(kind="text_present", text=node)
    if not isinstance(node, dict):
        raise ValueError(f"cannot parse condition from {node!r}")
    if "text" in node:
        return Condition(kind="text_present", text=node["text"],
                         match=node.get("match", "contains"))
    if "not_text" in node:
        return Condition(kind="text_absent", text=node["not_text"],
                         match=node.get("match", "contains"))
    if "url" in node:
        return Condition(kind="url_matches", pattern=node["url"])
    if "control" in node:
        return Condition(kind="control_present", control=parse_control(node["control"]))
    if "any" in node:
        return Condition(kind="any_of", operands=[parse_condition(o) for o in node["any"]])
    if "all" in node:
        return Condition(kind="all_of", operands=[parse_condition(o) for o in node["all"]])
    if "not" in node:
        return Condition(kind="not", operands=[parse_condition(node["not"])])
    raise ValueError(f"unrecognised condition keys: {sorted(node)}")


def parse_control(node: dict[str, Any]) -> ControlDescriptor:
    """Expand `{role: button, label: "Acknowledge"}` into a ranked descriptor.

    Hand-authored controls get the two portable strategies only. If a profile
    author needs a DOM fallback they are describing something that should have
    been recorded, not hand-written.
    """
    role, label = node.get("role"), node.get("label")
    strategies: list[LocatorStrategy] = []
    if label:
        # label_text first, deliberately. On these apps most controls have no
        # accessible name at all and the label is recovered from layout, so
        # ranking ax_role_name first would make every hand-authored control
        # resolve by "fallback" and report drift that is not happening.
        strategies.append(LocatorStrategy(
            kind=LocatorKind.LABEL_TEXT, rank=0, confidence=0.9,
            role=role, label=NameMatch(value=label)))
        strategies.append(LocatorStrategy(
            kind=LocatorKind.AX_ROLE_NAME, rank=1, confidence=0.8,
            role=role, name=NameMatch(value=label)))
    return ControlDescriptor(
        description=node.get("description") or f"{role or 'control'} {label!r}",
        strategies=strategies,
    )


def parse_step(node: dict[str, Any], index: int) -> Step:
    return Step(
        index=index,
        intent=node.get("intent", ""),
        action=ActionKind(node["action"]),
        target=parse_control(node["control"]) if node.get("control") else None,
        risk=RiskTier(node.get("risk", "safe")),
        optional=bool(node.get("optional", False)),
    )


@dataclass
class ProductProfile:
    """Vendor-product knowledge shared by every capability recorded against it."""
    product: str
    version_range: str = ""
    outcomes: list[OutcomeSpec] = field(default_factory=list)
    irreversible_labels: list[str] = field(default_factory=list)
    elevated_labels: list[str] = field(default_factory=list)

    def outcome(self, code: str) -> OutcomeSpec | None:
        return next((o for o in self.outcomes if o.code == code), None)

    def applicable(self, codes: list[str] | None = None) -> list[OutcomeSpec]:
        """All outcomes, or the named subset -- a capability may narrow the set
        (a read-only lookup cannot produce a posting rejection)."""
        if codes is None:
            return list(self.outcomes)
        return [o for o in self.outcomes if o.code in codes]


def load_product(path: str | pathlib.Path) -> ProductProfile:
    data = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    flows = data.get("recovery_flows") or {}

    def build_recovery(node: dict[str, Any]) -> RecoverySpec:
        steps_src = node.get("steps")
        if steps_src is None and node.get("flow"):
            flow = flows.get(node["flow"])
            if flow is None:
                raise KeyError(f"recovery flow {node['flow']!r} is not defined")
            steps_src = flow.get("steps") or []
        return RecoverySpec(
            steps=[parse_step(s, i) for i, s in enumerate(steps_src or [])],
            retry_step=bool(node.get("retry_step", True)),
            max_attempts=int(node.get("max_attempts", 2)),
        )

    outcomes: list[OutcomeSpec] = []
    for entry in data.get("outcomes") or []:
        classification = OutcomeClass(entry["classification"])
        outcomes.append(OutcomeSpec(
            code=entry["code"],
            classification=classification,
            detect=parse_condition(entry["detect"]),
            message=entry.get("message", ""),
            recovery=build_recovery(entry["recovery"]) if entry.get("recovery") else None,
        ))
    risk = data.get("risk_labels") or {}
    return ProductProfile(
        product=data.get("product", "unknown"),
        version_range=data.get("version_range", ""),
        outcomes=outcomes,
        irreversible_labels=list(risk.get("irreversible") or []),
        elevated_labels=list(risk.get("elevated") or []),
    )
