"""How a capability says "this control, the one I mean".

A ControlDescriptor is not a selector. It is a *bundle of ranked hypotheses*
about how to find one control, recorded at discovery time, tried in order at
replay time. Replay records which hypothesis actually won, which is what gives
us a drift signal without anyone writing a drift detector.
"""
from __future__ import annotations

from pydantic import BaseModel, Field

from .enums import LocatorKind


class NameMatch(BaseModel):
    """How literally to take a recorded accessible name.

    Legacy apps pad labels with colons, non-breaking spaces and casing changes,
    and the same vendor product renames "Member ID" to "Member Number" between
    tenants. `normalized` (casefold + collapse whitespace + strip punctuation)
    is the default because exact matching on these strings is a trap.
    """
    mode: str = Field("normalized", pattern="^(exact|normalized|contains|regex)$")
    value: str


class AnchorSpec(BaseModel):
    """Find a control by its position relative to text that identifies the row.

    This is how you target a balance in a borderless table with no IDs: not
    "the 4th cell of the 2nd row" (breaks when a row is added) but "the cell in
    the Current Balance column of the row whose Type cell reads SAVINGS".
    """
    #: Text that identifies the containing row/group.
    anchor_text: str
    anchor_match: str = Field("normalized", pattern="^(exact|normalized|contains|regex)$")
    #: Which structural container to climb to before searching (web: tr, li, fieldset).
    container_role: str = "row"
    #: Column identified by its header text -- survives column reordering.
    column_header: str | None = None
    #: Positional fallback within the container, used only if column_header is absent.
    cell_index: int | None = None


class LocatorStrategy(BaseModel):
    """One hypothesis for finding a control."""
    kind: LocatorKind
    #: Lower rank is tried first. Recorded, not hardcoded, so a capability can be
    #: hand-tuned for a tenant whose DOM happens to be better than its labels.
    rank: int = 0
    #: Confidence assigned at record time from how uniquely this matched.
    confidence: float = Field(0.5, ge=0.0, le=1.0)

    # --- kind-specific payload (only the relevant fields are populated) ---
    role: str | None = None
    name: NameMatch | None = None
    label: NameMatch | None = None
    anchor: AnchorSpec | None = None
    attr: str | None = None          # DOM_ATTR: attribute name, e.g. "name"
    attr_value: str | None = None    # DOM_ATTR: expected value
    css: str | None = None           # DOM_CSS
    ordinal: int | None = None       # ORDINAL: 0-based index among role matches
    within_text: str | None = None   # ORDINAL: region hint

    def describe(self) -> str:
        if self.kind is LocatorKind.AX_ROLE_NAME:
            return f"{self.role}[name~={self.name.value!r}]" if self.name else f"{self.role}"
        if self.kind is LocatorKind.LABEL_TEXT:
            return f"{self.role} labelled {self.label.value!r}" if self.label else "labelled control"
        if self.kind is LocatorKind.ANCHOR_RELATIVE and self.anchor:
            col = self.anchor.column_header or f"cell[{self.anchor.cell_index}]"
            return f"{col} of row containing {self.anchor.anchor_text!r}"
        if self.kind is LocatorKind.DOM_ATTR:
            return f"[{self.attr}={self.attr_value!r}]"
        if self.kind is LocatorKind.DOM_CSS:
            return f"css={self.css!r}"
        if self.kind is LocatorKind.ORDINAL:
            return f"{self.role}#{self.ordinal}"
        return self.kind.value


class ControlDescriptor(BaseModel):
    """The full targeting contract for one control."""
    #: Human-readable, for reviewers and for error messages. Not used to match.
    description: str
    #: Ranked hypotheses. Replay tries these in rank order and stops at the
    #: first that resolves to exactly one control.
    strategies: list[LocatorStrategy] = Field(default_factory=list)
    #: Which frame the control lives in. Empty list = top document.
    frame_path: list[str] = Field(default_factory=list)
    #: If true, a resolution that needed a low-ranked strategy is reported but
    #: not failed. If false, falling back past rank 1 is itself a warning.
    allow_fallback: bool = True

    def ordered(self) -> list[LocatorStrategy]:
        return sorted(self.strategies, key=lambda s: (s.rank, -s.confidence))

    @property
    def primary(self) -> LocatorStrategy | None:
        o = self.ordered()
        return o[0] if o else None
