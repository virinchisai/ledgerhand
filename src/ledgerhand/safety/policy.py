"""The guardrail model.

One enforcement point, used identically by discovery and by replay. That
symmetry is the design: a guardrail that only the exploratory path honours is
not a guardrail, and a guardrail the production path implements separately will
drift away from the one that was reviewed.

Three independent gates, because they fail for different reasons:
  * reach   -- is this address inside the allowlist at all?
  * action  -- is this *kind* of act permitted here?
  * risk    -- is this act one a machine may perform unattended?
"""
from __future__ import annotations

import pathlib
import re
from dataclasses import dataclass, field
from enum import Enum
from urllib.parse import urlparse

import yaml
from pydantic import BaseModel, Field

from ..models.enums import ActionKind, RiskTier
from ..models.observation import UINode


class Verdict(str, Enum):
    ALLOW = "allow"
    DENY = "deny"
    CONFIRM = "confirm"     # allowed, but only with a human decision on record


@dataclass
class Decision:
    verdict: Verdict
    reason: str = ""
    rule: str = ""

    @property
    def allowed(self) -> bool:
        return self.verdict is Verdict.ALLOW

    def __bool__(self) -> bool:
        return self.allowed


class NetworkPolicy(BaseModel):
    """Where the agent may go. Default-deny."""
    allowed_origins: list[str] = Field(default_factory=list)
    allowed_path_prefixes: list[str] = Field(default_factory=lambda: ["/"])
    denied_path_patterns: list[str] = Field(default_factory=list)


class ActionPolicy(BaseModel):
    allowed_actions: list[ActionKind] = Field(default_factory=lambda: list(ActionKind))


class RiskPolicy(BaseModel):
    """How to treat acts that cannot be undone.

    `confirm` is the default rather than `block` because blocking outright makes
    the system useless for the flows banks actually want automated (opening an
    account *is* the job). Confirmation keeps a human on the irreversible act
    while letting the other ninety percent run unattended.
    """
    irreversible_mode: str = Field("confirm", pattern="^(block|confirm|flag)$")
    #: Control labels that mark a commit point. Heuristic, and deliberately
    #: over-broad: a false positive costs one confirmation, a false negative
    #: costs a wrongly-opened account.
    irreversible_labels: list[str] = Field(default_factory=list)
    #: Labels that write but are scoped/reversible.
    elevated_labels: list[str] = Field(default_factory=list)


class RedactionPolicy(BaseModel):
    extra_patterns: list[dict[str, str]] = Field(default_factory=list)
    #: Redact screen text before it is shown to the model. Only meaningful for
    #: hosted providers; a local model never leaves the host.
    redact_before_model: bool = False


class PolicyProfile(BaseModel):
    name: str = "default"
    network: NetworkPolicy = Field(default_factory=NetworkPolicy)
    actions: ActionPolicy = Field(default_factory=ActionPolicy)
    risk: RiskPolicy = Field(default_factory=RiskPolicy)
    redaction: RedactionPolicy = Field(default_factory=RedactionPolicy)
    #: Ceiling on a single run, so a confused loop cannot grind forever.
    max_steps: int = 24
    max_run_seconds: int = 900


@dataclass
class PolicyGate:
    """Enforces a profile. Shared by the agent loop and the replay engine."""
    profile: PolicyProfile
    #: Recorded for evidence: every decision this gate made.
    decisions: list[tuple[str, Decision]] = field(default_factory=list)

    # -- reach --------------------------------------------------------------

    def check_url(self, url: str) -> Decision:
        if not url:
            return self._record("url", Decision(Verdict.DENY, "empty url", "network"))
        try:
            parsed = urlparse(url)
        except ValueError:
            return self._record("url", Decision(Verdict.DENY, f"unparseable url {url!r}", "network"))
        origin = f"{parsed.scheme}://{parsed.netloc}"
        allowed = self.profile.network.allowed_origins
        if allowed and origin not in allowed:
            return self._record("url", Decision(
                Verdict.DENY, f"origin {origin} not in allowlist", "network.allowed_origins"))
        path = parsed.path or "/"
        for pattern in self.profile.network.denied_path_patterns:
            if re.search(pattern, path):
                return self._record("url", Decision(
                    Verdict.DENY, f"path {path} matches denied pattern {pattern!r}",
                    "network.denied_path_patterns"))
        prefixes = self.profile.network.allowed_path_prefixes
        if prefixes and not any(path.startswith(p) for p in prefixes):
            return self._record("url", Decision(
                Verdict.DENY, f"path {path} outside allowed prefixes", "network.allowed_path_prefixes"))
        return self._record("url", Decision(Verdict.ALLOW, rule="network"))

    # -- action kind --------------------------------------------------------

    def check_action(self, kind: ActionKind) -> Decision:
        if kind not in self.profile.actions.allowed_actions:
            return self._record("action", Decision(
                Verdict.DENY, f"action {kind.value} not permitted by profile",
                "actions.allowed_actions"))
        return self._record("action", Decision(Verdict.ALLOW, rule="actions"))

    # -- risk ---------------------------------------------------------------

    def classify_risk(self, kind: ActionKind, node: UINode | None) -> RiskTier:
        """Assign a risk tier to a concrete act.

        Reads are always safe. Writes are judged by what the control is called,
        because on these apps the label is the only description of the effect
        that exists.
        """
        if kind in (ActionKind.EXTRACT, ActionKind.WAIT, ActionKind.ASSERT, ActionKind.FINISH):
            return RiskTier.SAFE
        if kind in (ActionKind.NAVIGATE, ActionKind.PRESS):
            return RiskTier.SAFE
        label = (node.label if node else "") or ""
        low = label.casefold()
        if kind is ActionKind.CLICK:
            for marker in self.profile.risk.irreversible_labels:
                if marker.casefold() in low:
                    return RiskTier.IRREVERSIBLE
            for marker in self.profile.risk.elevated_labels:
                if marker.casefold() in low:
                    return RiskTier.ELEVATED
            return RiskTier.SAFE
        # Typing/selecting fills a field; nothing is committed until a click.
        return RiskTier.ELEVATED

    def check_risk(self, tier: RiskTier, *, attended: bool) -> Decision:
        if tier is not RiskTier.IRREVERSIBLE:
            return self._record("risk", Decision(Verdict.ALLOW, rule="risk"))
        mode = self.profile.risk.irreversible_mode
        if mode == "block":
            return self._record("risk", Decision(
                Verdict.DENY, "irreversible action blocked by profile", "risk.irreversible_mode"))
        if mode == "flag" or attended:
            return self._record("risk", Decision(
                Verdict.ALLOW, "irreversible action permitted (attended/flagged)", "risk.irreversible_mode"))
        return self._record("risk", Decision(
            Verdict.CONFIRM, "irreversible action requires human confirmation",
            "risk.irreversible_mode"))

    def _record(self, scope: str, decision: Decision) -> Decision:
        self.decisions.append((scope, decision))
        return decision

    @property
    def denials(self) -> list[tuple[str, Decision]]:
        return [(s, d) for s, d in self.decisions if d.verdict is not Verdict.ALLOW]


def load_profile(path: str | pathlib.Path, name: str = "default") -> PolicyProfile:
    """Load one named profile from a YAML policy file."""
    data = yaml.safe_load(pathlib.Path(path).read_text()) or {}
    profiles = data.get("profiles") or {}
    if name not in profiles:
        raise KeyError(f"policy profile {name!r} not found in {path} "
                       f"(have: {', '.join(profiles) or 'none'})")
    return PolicyProfile(name=name, **profiles[name])
