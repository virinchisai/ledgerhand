"""Evaluating conditions against an observation.

Pure, like the resolver: an Observation plus its text in, a boolean out. No
browser, no I/O. Checkpoints, success conditions and outcome detectors all run
through here, so there is exactly one semantics to reason about and to test.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from ..models.conditions import Condition
from ..models.observation import Observation
from .locators import normalize, resolve


@dataclass
class Evaluation:
    value: bool
    #: Why, in one line, for the failure report.
    detail: str


def _text_pool(obs: Observation, page_text: str | None) -> str:
    """All readable text. Includes node text because a value rendered inside a
    table cell may not appear in the frame's innerText we were handed."""
    parts = [page_text or "", "\n".join(obs.texts)]
    parts += [n.text for n in obs.controls if n.text]
    parts += [n.value for n in obs.controls if n.value]
    return "\n".join(p for p in parts if p)


def _contains(haystack: str, needle: str, mode: str) -> bool:
    if mode == "exact":
        return needle in haystack
    if mode == "regex":
        try:
            return re.search(needle, haystack, re.I | re.S) is not None
        except re.error:
            return False
    # normalized / contains both fold case and whitespace before comparing
    return normalize(needle) in normalize(haystack)


def evaluate(cond: Condition, obs: Observation, page_text: str | None = None) -> Evaluation:
    kind = cond.kind

    if kind in ("text_present", "text_absent"):
        pool = _text_pool(obs, page_text)
        found = _contains(pool, cond.text or "", cond.match)
        want = kind == "text_present"
        return Evaluation(found == want,
                          f"{'found' if found else 'did not find'} {cond.text!r} on screen")

    if kind in ("control_present", "control_absent"):
        if cond.control is None:
            return Evaluation(False, "condition has no control")
        res = resolve(cond.control, obs)
        want = kind == "control_present"
        return Evaluation(res.ok == want,
                          f"{cond.control.description} {'present' if res.ok else 'absent'}")

    if kind == "url_matches":
        try:
            hit = re.search(cond.pattern or "", obs.url) is not None
        except re.error:
            return Evaluation(False, f"invalid url pattern {cond.pattern!r}")
        return Evaluation(hit, f"url {obs.url!r} {'matches' if hit else 'does not match'} {cond.pattern!r}")

    if kind == "value_matches":
        if cond.control is None:
            return Evaluation(False, "condition has no control")
        res = resolve(cond.control, obs)
        if not res.ok or res.node is None:
            return Evaluation(False, f"{cond.control.description} not found")
        actual = res.node.value or res.node.text
        try:
            hit = re.search(cond.pattern or "", actual or "") is not None
        except re.error:
            return Evaluation(False, f"invalid value pattern {cond.pattern!r}")
        return Evaluation(hit, f"value {actual!r} {'matches' if hit else 'does not match'} {cond.pattern!r}")

    if kind == "all_of":
        results = [evaluate(o, obs, page_text) for o in cond.operands]
        bad = [r for r in results if not r.value]
        return Evaluation(not bad, "; ".join(r.detail for r in (bad or results)))

    if kind == "any_of":
        results = [evaluate(o, obs, page_text) for o in cond.operands]
        good = next((r for r in results if r.value), None)
        return Evaluation(good is not None,
                          good.detail if good else "; ".join(r.detail for r in results))

    if kind == "not":
        inner = evaluate(cond.operands[0], obs, page_text) if cond.operands else Evaluation(True, "")
        return Evaluation(not inner.value, f"not({inner.detail})")

    return Evaluation(False, f"unsupported condition kind {kind!r}")
