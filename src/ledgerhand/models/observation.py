"""What the agent sees. Surface-independent by construction.

A UINode is deliberately *not* a DOM element. It carries only things that also
exist in a macOS AX tree or a Windows UIA tree: a role, an accessible name, a
value, state flags, geometry, and a position in a frame/window hierarchy. The
web driver is one producer of these; a desktop driver would be another, and
neither the agent nor the replay engine can tell the difference.
"""
from __future__ import annotations

from pydantic import BaseModel, Field


class UINode(BaseModel):
    """One perceivable, addressable control or piece of text."""
    #: Stable-within-observation handle the agent refers to ("e7").
    handle: str
    role: str
    #: Accessible name as the platform computes it. Often empty on legacy web.
    name: str = ""
    #: Label recovered from layout when `name` is empty (legacy table cells,
    #: preceding text). This is the bridge that makes old markup addressable.
    inferred_label: str = ""
    value: str = ""
    #: Text content, for static/readable nodes.
    text: str = ""
    enabled: bool = True
    focused: bool = False
    editable: bool = False
    #: x, y, w, h in surface coordinates. Used for screenshot evidence and as
    #: the acting mechanism on surfaces with no other addressing.
    box: tuple[int, int, int, int] | None = None
    #: Frame/window chain from the root. [] means top-level document.
    frame_path: list[str] = Field(default_factory=list)
    #: Driver-specific extras (web: the generated `name` attribute, tag, css).
    attrs: dict[str, str] = Field(default_factory=dict)

    @property
    def label(self) -> str:
        """The single best human-facing string for this control."""
        return self.name or self.inferred_label or self.value or self.text

    def render(self) -> str:
        """One compact line for the model's prompt.

        Compactness is not cosmetic here: on a CPU-bound local model, prompt
        processing dominates latency, so the observation format is a
        performance decision as much as a prompting one.
        """
        bits = [f"[{self.handle}] {self.role}"]
        if self.label:
            bits.append(f'"{self.label[:60]}"')
        if self.value and self.value != self.label:
            bits.append(f"value={self.value[:40]!r}")
        if not self.enabled:
            bits.append("(disabled)")
        return " ".join(bits)


class Observation(BaseModel):
    """One perception of the surface at a point in time."""
    step: int
    url: str = ""
    title: str = ""
    #: Interactive controls the agent may act on.
    controls: list[UINode] = Field(default_factory=list)
    #: Readable text blocks, already trimmed to what fits a prompt.
    texts: list[str] = Field(default_factory=list)
    #: Frames present, for the agent's awareness and the recorder's frame_path.
    frames: list[str] = Field(default_factory=list)
    #: Address of the innermost content frame. In a framed legacy app the
    #: top-level URL is chrome and stays put while the user moves between
    #: screens -- reporting only that actively misleads anyone, human or model,
    #: trying to work out where they are.
    content_url: str = ""
    #: Path to the screenshot captured alongside, if any.
    screenshot: str | None = None
    #: Set when the surface reported a transport-level problem (HTTP 500 etc).
    transport_error: str | None = None

    def node(self, handle: str) -> UINode | None:
        return next((n for n in self.controls if n.handle == handle), None)

    def render(self, *, max_texts: int = 18) -> str:
        lines = [f"URL: {self.url}", f"TITLE: {self.title}"]
        if self.content_url and self.content_url != self.url:
            lines.append(f"CONTENT: {self.content_url}")
        if self.frames:
            lines.append(f"FRAMES: {', '.join(self.frames)}")
        if self.transport_error:
            lines.append(f"TRANSPORT ERROR: {self.transport_error}")
        lines.append("SCREEN TEXT:")
        for t in self.texts[:max_texts]:
            lines.append(f"  {t}")
        lines.append("CONTROLS:")
        for c in self.controls:
            lines.append(f"  {c.render()}")
        return "\n".join(lines)
