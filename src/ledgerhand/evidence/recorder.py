"""Run evidence: a structured log plus richer signals when things go wrong.

Everything written here passes through the Redactor first. That is enforced in
one place -- `event()` -- rather than at every call site, because a redaction
rule that depends on every caller remembering it is not a rule.
"""
from __future__ import annotations

import json
import pathlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from ..safety.redaction import Redactor


@dataclass
class EvidenceWriter:
    """Append-only evidence for one run."""
    root: pathlib.Path
    run_id: str
    redactor: Redactor = field(default_factory=Redactor)
    #: Set false for dry runs / tests that should not litter the filesystem.
    enabled: bool = True
    _events: list[dict[str, Any]] = field(default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.root = pathlib.Path(self.root)
        if self.enabled:
            self.dir.mkdir(parents=True, exist_ok=True)
            (self.dir / "screens").mkdir(exist_ok=True)

    @property
    def dir(self) -> pathlib.Path:
        return self.root / self.run_id

    @property
    def log_path(self) -> pathlib.Path:
        return self.dir / "run.jsonl"

    def event(self, kind: str, **fields: Any) -> dict[str, Any]:
        """Record one structured event. Scrubbed on the way out, always."""
        record = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
            "run_id": self.run_id,
            "kind": kind,
            **fields,
        }
        clean = self.redactor.scrub_obj(record)
        assert isinstance(clean, dict)
        self._events.append(clean)
        if self.enabled:
            with self.log_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(clean, default=str) + "\n")
        return clean

    def screen_path(self, label: str) -> str:
        return str(self.dir / "screens" / f"{label}.png")

    def snapshot_path(self, label: str) -> str:
        return str(self.dir / "screens" / f"{label}.html")

    def write_json(self, name: str, obj: Any) -> str | None:
        if not self.enabled:
            return None
        path = self.dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = obj.model_dump(mode="json") if hasattr(obj, "model_dump") else obj
        path.write_text(json.dumps(self.redactor.scrub_obj(payload), indent=2, default=str),
                        encoding="utf-8")
        return str(path)

    def write_text(self, name: str, text: str) -> str | None:
        if not self.enabled:
            return None
        path = self.dir / name
        path.write_text(self.redactor.scrub(text), encoding="utf-8")
        return str(path)

    @property
    def events(self) -> list[dict[str, Any]]:
        return list(self._events)
