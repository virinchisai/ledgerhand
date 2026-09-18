"""Keeping regulated data out of everything we persist.

The rule this module enforces: an artifact, a log line and an evidence file are
all *durable* -- they get committed, shipped to a log aggregator, and read by
people who were never entitled to the underlying data. So values that are PII
or secret must not reach them, ever, including inside a model's own explanation
of what it was doing.

What this module is not: a guarantee about pixels. A screenshot of an account
detail screen contains PII by construction. That is handled by policy on where
evidence is stored, not by pattern matching, and it is called out as a limit.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

#: Patterns applied to every string before it is written anywhere.
#: Ordered most-specific first so a card number is not partly eaten by the
#: generic long-digit rule.
DEFAULT_PATTERNS: list[tuple[str, str]] = [
    ("ssn", r"\b\d{3}-\d{2}-\d{4}\b"),
    ("card", r"\b(?:\d[ -]?){13,19}\b"),
    ("email", r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b"),
    ("routing", r"\b\d{9}\b"),
]


@dataclass
class Redactor:
    """Redacts by pattern and by known literal secret values."""
    patterns: list[tuple[str, str]] = field(default_factory=lambda: list(DEFAULT_PATTERNS))
    #: Literal values registered at runtime (resolved secrets, PII arguments).
    #: Never serialised; lives only for the process.
    _literals: dict[str, str] = field(default_factory=dict, repr=False)
    _compiled: list[tuple[str, re.Pattern[str]]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self._compiled = [(name, re.compile(rx)) for name, rx in self.patterns]

    def register(self, value: str | None, kind: str = "secret") -> None:
        """Mark a concrete value as unloggable for the rest of the process.

        This is what catches the case patterns cannot: a password or a member ID
        that looks like ordinary text but must not be written down.
        """
        if value and len(str(value)) >= 3:
            self._literals[str(value)] = kind

    def scrub(self, text: str | None) -> str:
        if not text:
            return text or ""
        out = str(text)
        # Literals first: they are exact and must win over partial pattern hits.
        for literal, kind in sorted(self._literals.items(), key=lambda kv: -len(kv[0])):
            out = out.replace(literal, f"<redacted:{kind}>")
        for name, rx in self._compiled:
            out = rx.sub(f"<redacted:{name}>", out)
        return out

    def scrub_obj(self, obj: object) -> object:
        """Recursively scrub a JSON-ish structure."""
        if isinstance(obj, str):
            return self.scrub(obj)
        if isinstance(obj, dict):
            return {k: self.scrub_obj(v) for k, v in obj.items()}
        if isinstance(obj, (list, tuple)):
            return [self.scrub_obj(v) for v in obj]
        return obj
