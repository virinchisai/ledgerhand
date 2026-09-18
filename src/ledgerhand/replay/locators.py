"""Turning nodes into descriptors (recording) and descriptors back into nodes
(replay). These are inverse operations so they live together.

The resolver is a *pure function over an Observation*. It never touches a
browser. That is deliberate: it means locator behaviour -- including every drift
and ambiguity case -- is unit-testable without launching anything, and it means
the same resolver serves a desktop driver unchanged.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models.enums import LocatorKind
from ..models.locator import AnchorSpec, ControlDescriptor, LocatorStrategy, NameMatch
from ..models.observation import Observation, UINode

_PUNCT = re.compile(r"[\s:* ]+")


def normalize(text: str) -> str:
    """Fold the differences that are noise on legacy UIs.

    Case, runs of whitespace, non-breaking spaces and trailing label punctuation
    all vary between tenants and versions of the same vendor screen without the
    control meaning anything different.
    """
    return _PUNCT.sub(" ", (text or "").replace(" ", " ")).strip().casefold().rstrip(":* ")


def matches(candidate: str, spec: NameMatch) -> bool:
    mode, want = spec.mode, spec.value
    if mode == "exact":
        return candidate == want
    if mode == "contains":
        return normalize(want) in normalize(candidate)
    if mode == "regex":
        try:
            return re.search(want, candidate or "", re.I) is not None
        except re.error:
            return False
    return normalize(candidate) == normalize(want)


@dataclass
class Resolution:
    """What happened when we tried to find a control."""
    node: UINode | None = None
    strategy: LocatorStrategy | None = None
    rank: int = -1
    candidates: int = 0
    #: One line per strategy tried, for the failure report.
    attempts: list[str] = field(default_factory=list)
    ambiguous: bool = False

    @property
    def ok(self) -> bool:
        return self.node is not None

    @property
    def used_fallback(self) -> bool:
        """True when the preferred strategy no longer works. The drift signal."""
        return self.ok and self.rank > 0


# ---------------------------------------------------------------------------
# Matching, one function per strategy kind
# ---------------------------------------------------------------------------

def _by_ax_role_name(nodes: list[UINode], s: LocatorStrategy) -> list[UINode]:
    out = []
    for n in nodes:
        if s.role and n.role != s.role:
            continue
        if s.name and not matches(n.name, s.name):
            continue
        out.append(n)
    return out


def _by_label_text(nodes: list[UINode], s: LocatorStrategy) -> list[UINode]:
    """Match the best human-facing string, whichever source it came from."""
    out = []
    for n in nodes:
        if s.role and n.role != s.role:
            continue
        if s.label and not (matches(n.inferred_label, s.label) or matches(n.name, s.label)):
            continue
        out.append(n)
    return out


def _by_anchor(nodes: list[UINode], s: LocatorStrategy) -> list[UINode]:
    """'The Current Balance cell of the row whose Type is SAVINGS.'

    Addressing by row content and column *header* rather than by index is what
    survives a vendor adding a column in the next release.
    """
    anchor = s.anchor
    if anchor is None:
        return []
    out = []
    for n in nodes:
        row = n.attrs.get("row_text", "")
        if not row:
            continue
        if not matches(row, NameMatch(mode="contains", value=anchor.anchor_text)):
            continue
        if anchor.column_header:
            if normalize(n.attrs.get("col_header", "")) != normalize(anchor.column_header):
                continue
        elif anchor.cell_index is not None:
            if n.attrs.get("col_index") != str(anchor.cell_index):
                continue
        if s.role and n.role != s.role:
            continue
        out.append(n)
    return out


def _by_dom_attr(nodes: list[UINode], s: LocatorStrategy) -> list[UINode]:
    if not s.attr:
        return []
    return [n for n in nodes if n.attrs.get(s.attr) == s.attr_value]


def _by_dom_css(nodes: list[UINode], s: LocatorStrategy) -> list[UINode]:
    return [n for n in nodes if s.css and n.attrs.get("css") == s.css]


def _by_ordinal(nodes: list[UINode], s: LocatorStrategy) -> list[UINode]:
    pool = [n for n in nodes if not s.role or n.role == s.role]
    if s.within_text:
        pool = [n for n in pool if normalize(s.within_text) in normalize(n.attrs.get("row_text", ""))]
    idx = s.ordinal or 0
    return [pool[idx]] if 0 <= idx < len(pool) else []


_MATCHERS = {
    LocatorKind.AX_ROLE_NAME: _by_ax_role_name,
    LocatorKind.LABEL_TEXT: _by_label_text,
    LocatorKind.ANCHOR_RELATIVE: _by_anchor,
    LocatorKind.DOM_ATTR: _by_dom_attr,
    LocatorKind.DOM_CSS: _by_dom_css,
    LocatorKind.ORDINAL: _by_ordinal,
}


# ---------------------------------------------------------------------------
# Resolve
# ---------------------------------------------------------------------------

def resolve(descriptor: ControlDescriptor, observation: Observation) -> Resolution:
    """Try each recorded hypothesis in rank order; first unique match wins."""
    res = Resolution()
    ordered = descriptor.ordered()
    if not ordered:
        res.attempts.append("no strategies recorded")
        return res

    # Frame is a preference, not a filter: frame names drift, and a control that
    # moved between frames is still the right control.
    preferred = [n for n in observation.controls if n.frame_path == descriptor.frame_path]
    pools = [(preferred, "")] if preferred else []
    pools.append((observation.controls, " (any frame)"))

    for rank, strategy in enumerate(ordered):
        matcher = _MATCHERS.get(strategy.kind)
        if matcher is None:
            res.attempts.append(f"rank {rank} {strategy.kind.value}: unsupported")
            continue
        for pool, suffix in pools:
            found = matcher(pool, strategy)
            if len(found) == 1:
                res.node = found[0]
                res.strategy = strategy
                res.rank = rank
                res.candidates = 1
                res.attempts.append(f"rank {rank} {strategy.describe()}: matched{suffix}")
                return res
            if len(found) > 1:
                res.ambiguous = True
                res.attempts.append(
                    f"rank {rank} {strategy.describe()}: ambiguous, {len(found)} matches{suffix}")
                break
        else:
            res.attempts.append(f"rank {rank} {strategy.describe()}: no match")
    return res


# ---------------------------------------------------------------------------
# Record -- the inverse
# ---------------------------------------------------------------------------

def describe_control(
    node: UINode,
    observation: Observation,
    *,
    description: str | None = None,
) -> ControlDescriptor:
    """Build a ranked ControlDescriptor for a node the agent just used.

    Ranking is not fixed: each candidate strategy is *tested against the
    observation it was recorded in* and kept only if it uniquely identifies the
    node. Confidence is assigned from that test. A strategy that is already
    ambiguous at record time would certainly be ambiguous at replay, so it is
    demoted rather than written out as if it were sound.
    """
    pool = observation.controls
    candidates: list[tuple[float, LocatorStrategy]] = []

    def unique(strategy: LocatorStrategy) -> bool:
        found = _MATCHERS[strategy.kind](pool, strategy)
        return len(found) == 1 and found[0].handle == node.handle

    if node.name:
        s = LocatorStrategy(kind=LocatorKind.AX_ROLE_NAME, role=node.role,
                            name=NameMatch(value=node.name))
        if unique(s):
            candidates.append((0.95, s))

    label = node.inferred_label or node.name
    if label:
        s = LocatorStrategy(kind=LocatorKind.LABEL_TEXT, role=node.role,
                            label=NameMatch(value=label))
        if unique(s):
            candidates.append((0.85, s))

    row_text, col_header = node.attrs.get("row_text"), node.attrs.get("col_header")
    if row_text:
        # Anchor on the most distinctive cell in the row, not the whole row.
        anchor_text = _distinctive_cell(row_text, skip=node.text)
        if col_header:
            # Best case: a real column header. Survives column reordering.
            anchor = AnchorSpec(anchor_text=anchor_text, column_header=col_header)
            confidence = 0.8
        else:
            # Layout tables often have no header row at all. Position within
            # the row is still far more durable than a CSS path, because it
            # survives everything except the vendor reordering that one row.
            index = node.attrs.get("col_index")
            anchor = AnchorSpec(anchor_text=anchor_text,
                                cell_index=int(index) if index and index.isdigit() else None)
            confidence = 0.7
        s = LocatorStrategy(kind=LocatorKind.ANCHOR_RELATIVE, role=node.role, anchor=anchor)
        if unique(s):
            candidates.append((confidence, s))

    if node.attrs.get("name"):
        s = LocatorStrategy(kind=LocatorKind.DOM_ATTR, attr="name",
                            attr_value=node.attrs["name"])
        if unique(s):
            candidates.append((0.6, s))

    if node.attrs.get("css"):
        s = LocatorStrategy(kind=LocatorKind.DOM_CSS, css=node.attrs["css"])
        if unique(s):
            candidates.append((0.35, s))

    if not candidates:
        # Nothing identifies it but its position. Recorded honestly, with the
        # low confidence that implies, so review can catch it.
        same_role = [n for n in pool if n.role == node.role]
        idx = next((i for i, n in enumerate(same_role) if n.handle == node.handle), 0)
        candidates.append((0.2, LocatorStrategy(kind=LocatorKind.ORDINAL,
                                                role=node.role, ordinal=idx)))

    candidates.sort(key=lambda c: -c[0])
    strategies = [
        s.model_copy(update={"rank": rank, "confidence": conf})
        for rank, (conf, s) in enumerate(candidates)
    ]
    return ControlDescriptor(
        description=description or _describe(node),
        strategies=strategies,
        frame_path=list(node.frame_path),
    )


#: Row cells whose value carries no identifying information.
_GENERIC_CELLS = frozenset({"active", "closed", "pending", "open", "yes", "no", "-", "n/a"})


def _distinctive_cell(row_text: str, *, skip: str = "") -> str:
    """Pick the cell that identifies a row *across invocations*.

    The subtlety that matters: the most eye-catching cell is usually the wrong
    anchor. In a row like `0001-4471 | SAVINGS | ACTIVE | 4,182.55` the account
    number is the most distinctive string, and it is also per-record -- anchor
    on it and the capability works for exactly one member. `SAVINGS` comes from
    a closed vocabulary the vendor controls, so it is the same on every record.

    So: prefer cells with no digits, skip the value being read and generic
    status words, and only fall back to raw length when nothing qualifies.
    """
    cells = [c.strip() for c in row_text.split("|") if c.strip()]
    usable = [
        c for c in cells
        if normalize(c) != normalize(skip) and normalize(c) not in _GENERIC_CELLS
    ]
    if not usable:
        return cells[0] if cells else row_text
    categorical = [c for c in usable if not any(ch.isdigit() for ch in c)]
    return max(categorical or usable, key=len)


#: Roles whose visible text is page content, not a control label.
_CONTENT_ROLES = frozenset({"cell", "columnheader", "text", "generic"})


def _describe(node: UINode) -> str:
    """A description a reviewer can read that does not quote record content.

    Control labels ("Member ID") are page furniture and safe to name. The text
    inside a data cell is the customer's data, and this string ends up in an
    artifact that gets committed and in error messages that get logged -- so
    content nodes are described by where they sit, never by what they say.
    """
    if node.role not in _CONTENT_ROLES:
        label = node.label or node.attrs.get("name") or node.handle
        return f"{node.role} {label!r}".strip()
    column = node.attrs.get("col_header")
    row = node.attrs.get("row_text")
    if column and row:
        return f"{column} cell of the {_distinctive_cell(row, skip=node.text)!r} row"
    if row:
        index = node.attrs.get("col_index", "?")
        return f"cell {index} of the {_distinctive_cell(row, skip=node.text)!r} row"
    return f"{node.role} value"
