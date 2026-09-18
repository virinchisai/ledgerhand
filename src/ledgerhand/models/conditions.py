"""A tiny composable boolean language over observed UI state.

Used for three things that are really one thing: step checkpoints ("did the
click work?"), success conditions ("did we reach the goal?"), and outcome
detectors ("is this a not-found?"). Keeping them one type means the replay
engine has one evaluator and reviewers have one thing to read.
"""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from .locator import ControlDescriptor

ConditionKind = Literal[
    "text_present", "text_absent", "control_present", "control_absent",
    "url_matches", "value_matches", "all_of", "any_of", "not",
]


class Condition(BaseModel):
    kind: ConditionKind
    #: text_present / text_absent
    text: str | None = None
    match: str = Field("normalized", pattern="^(exact|normalized|contains|regex)$")
    #: control_present / control_absent / value_matches
    control: ControlDescriptor | None = None
    #: url_matches / value_matches
    pattern: str | None = None
    #: all_of / any_of / not
    operands: list["Condition"] = Field(default_factory=list)
    #: Reviewer-facing explanation of what this assertion is really checking.
    note: str | None = None

    def describe(self) -> str:
        if self.kind in ("text_present", "text_absent"):
            return f"{self.kind}({self.text!r})"
        if self.kind in ("control_present", "control_absent"):
            return f"{self.kind}({self.control.description if self.control else '?'})"
        if self.kind == "url_matches":
            return f"url~={self.pattern!r}"
        if self.kind == "value_matches":
            tgt = self.control.description if self.control else "?"
            return f"value({tgt})~={self.pattern!r}"
        inner = ", ".join(o.describe() for o in self.operands)
        return f"{self.kind}({inner})"


Condition.model_rebuild()


def text_present(text: str, *, note: str | None = None) -> Condition:
    return Condition(kind="text_present", text=text, note=note)


def text_absent(text: str, *, note: str | None = None) -> Condition:
    return Condition(kind="text_absent", text=text, note=note)


def all_of(*ops: Condition, note: str | None = None) -> Condition:
    return Condition(kind="all_of", operands=list(ops), note=note)


def any_of(*ops: Condition, note: str | None = None) -> Condition:
    return Condition(kind="any_of", operands=list(ops), note=note)
