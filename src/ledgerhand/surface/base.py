"""The surface seam.

Everything above this line -- the agent loop, the artifact, the replay engine,
the escalation machinery -- is written against this protocol and has no idea
whether it is driving a browser, a Win32 app or a terminal emulator.

The protocol is deliberately narrow. It says *perceive the current state* and
*perform one semantic action on one perceived node*. It does not expose
selectors, DOM handles, windows or coordinates, because those are the things
that differ between surfaces.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from ..models.enums import ActionKind, SurfaceKind
from ..models.observation import Observation, UINode


@dataclass
class ActRequest:
    """One semantic action. `node` is always a node from the latest Observation."""
    kind: ActionKind
    node: UINode | None = None
    value: str | None = None
    url: str | None = None
    key: str | None = None
    #: Redaction hint: never let this value reach a log or screenshot caption.
    sensitive: bool = False


@dataclass
class ActResult:
    ok: bool
    detail: str = ""
    error: str | None = None
    #: Transport/application-level signal the driver noticed (HTTP status etc).
    transport_error: str | None = None


@dataclass
class SurfaceCaps:
    """What this driver can actually do, so callers degrade instead of guessing."""
    can_screenshot: bool = True
    can_snapshot_markup: bool = True
    can_coordinate_act: bool = True
    supports_frames: bool = True
    extras: dict[str, str] = field(default_factory=dict)


@runtime_checkable
class Surface(Protocol):
    """A driveable application surface."""
    kind: SurfaceKind

    def start(self) -> None: ...
    def close(self) -> None: ...
    def caps(self) -> SurfaceCaps: ...

    def navigate(self, url: str) -> ActResult:
        """Enter the surface at an address. For desktop this is 'launch/focus'."""

    def perceive(self, step: int = 0) -> Observation:
        """Snapshot the current state as surface-independent nodes."""

    def act(self, request: ActRequest) -> ActResult:
        """Perform one semantic action."""

    def settle(self, timeout_ms: int = 12_000) -> None:
        """Block until the surface is quiescent, or the timeout elapses."""

    def current_url(self) -> str:
        """Address of the current state. Desktop drivers return a window path."""

    def page_text(self) -> str:
        """All readable text, for condition evaluation."""

    def screenshot(self, path: str) -> str | None: ...

    def snapshot(self, path: str) -> str | None:
        """Richer failure evidence -- markup dump, AX dump, whatever the driver has."""
