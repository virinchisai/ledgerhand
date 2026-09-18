"""A deterministic stand-in for the model, used by pipeline tests.

It reads the same prompt a real model gets and picks a control by label. That
makes the loop, the recorder and the replay engine testable in seconds instead
of minutes, and -- more usefully -- it makes the *failure* paths testable at
all, since a real model mostly succeeds and rarely produces the malformed
output or dead ends the loop has to survive.

This is a test double. The discovery run recorded under /evidence is driven by
a real LLM; see README.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from ledgerhand.agent.llm import LLMReply, _extract_json

_CONTROL = re.compile(r"\[(?P<handle>[fF]\d+_[et]\d+)\]\s+(?P<role>\S+)\s+\"(?P<label>[^\"]*)\"")


@dataclass
class OracleClient:
    """Chooses the next action from the visible controls, by label."""
    name: str = "oracle"
    calls: list[str] = field(default_factory=list)
    #: label substring -> what to do when that control is on screen, in priority order
    plan: list[tuple[str, dict]] = field(default_factory=list)
    done: set[str] = field(default_factory=set)
    #: output name -> text to look for, when answering the goal check
    bind: dict[str, str] = field(default_factory=dict)

    def decide(self, system: str, user: str, *, max_tokens: int = 110) -> LLMReply:
        self.calls.append(user)
        controls = [m.groupdict() for m in _CONTROL.finditer(user)]

        # The loop asks two different questions; answer whichever this is.
        if "VALUES WANTED:" in user:
            raw = json.dumps({
                name: self._find(controls, hint)
                for name, hint in (self.bind or {}).items()
            })
            return LLMReply(raw, _extract_json(raw), 0, self.name)

        for key, action in self.plan:
            if key in self.done:
                continue
            match = next((c for c in controls
                          if key.casefold() in c["label"].casefold()
                          and (action.get("role") is None or c["role"] == action["role"])), None)
            if match is None:
                continue
            self.done.add(key)
            payload = {k: v for k, v in action.items() if k not in ("role",)}
            if payload.get("action") != "finish":
                payload["target"] = match["handle"]
            else:
                payload["outputs"] = {
                    name: self._find(controls, hint)
                    for name, hint in (payload.pop("bind") or {}).items()
                }
            payload.setdefault("reason", f"act on {match['label']}")
            raw = json.dumps(payload)
            return LLMReply(raw, _extract_json(raw), 0, self.name)

        raw = json.dumps({"action": "give_up", "reason": "oracle has no applicable move"})
        return LLMReply(raw, _extract_json(raw), 0, self.name)

    @staticmethod
    def _find(controls: list[dict], hint: str) -> str:
        match = next((c for c in controls if hint.casefold() in c["label"].casefold()), None)
        return match["handle"] if match else "none"
