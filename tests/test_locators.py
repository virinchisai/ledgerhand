"""Locator recording and resolution. Pure -- no browser, no app."""
from __future__ import annotations

from ledgerhand.models.enums import LocatorKind
from ledgerhand.models.locator import AnchorSpec, ControlDescriptor, LocatorStrategy, NameMatch
from ledgerhand.models.observation import Observation, UINode
from ledgerhand.replay.locators import describe_control, normalize, resolve


def cell(handle, text, row, col="", index="0", role="cell"):
    return UINode(handle=handle, role=role, text=text,
                  attrs={"row_text": row, "col_header": col, "col_index": index})


def field(handle, label, name="", css="", role="textbox"):
    return UINode(handle=handle, role=role, inferred_label=label,
                  attrs={k: v for k, v in (("name", name), ("css", css)) if v})


ACCOUNTS = Observation(step=0, controls=[
    cell("t1", "0001-4471", "0001-4471 | SAVINGS | ACTIVE | 4,182.55", "Account", "0"),
    cell("t2", "SAVINGS", "0001-4471 | SAVINGS | ACTIVE | 4,182.55", "Type", "1"),
    cell("t3", "4,182.55", "0001-4471 | SAVINGS | ACTIVE | 4,182.55", "Current Balance", "3"),
    cell("t4", "912.10", "0002-1180 | CHECKING | ACTIVE | 912.10", "Current Balance", "3"),
])


def test_normalize_folds_the_noise_legacy_uis_produce():
    assert normalize("  Member ID: ") == normalize("MEMBER  id")
    assert normalize("Current  Balance*") == "current balance"


def test_label_beats_dom_when_both_are_available():
    obs = Observation(step=0, controls=[
        field("e1", "Member ID", name="ctl00$MainContent$txtMemberId", css="form > input")])
    d = describe_control(obs.controls[0], obs)
    kinds = [s.kind for s in d.ordered()]
    assert kinds[0] is LocatorKind.LABEL_TEXT
    assert LocatorKind.DOM_ATTR in kinds and kinds.index(LocatorKind.DOM_ATTR) > 0
    assert kinds[-1] is LocatorKind.DOM_CSS, "css must be the last resort, not the plan"


def test_anchor_prefers_a_categorical_cell_over_an_identifier():
    """An account number identifies one record; an account type identifies a row
    on every record. Anchoring on the former silently breaks for input #2."""
    d = describe_control(ACCOUNTS.node("t3"), ACCOUNTS)
    assert "SAVINGS" in d.primary.describe()
    assert "0001-4471" not in d.primary.describe()


def test_anchor_resolves_on_a_different_record():
    d = describe_control(ACCOUNTS.node("t3"), ACCOUNTS)
    other = Observation(step=0, controls=[
        cell("x1", "0007-2213", "0007-2213 | SAVINGS | ACTIVE | 27,430.09", "Account", "0"),
        cell("x2", "SAVINGS", "0007-2213 | SAVINGS | ACTIVE | 27,430.09", "Type", "1"),
        cell("x3", "27,430.09", "0007-2213 | SAVINGS | ACTIVE | 27,430.09", "Current Balance", "3"),
    ])
    res = resolve(d, other)
    assert res.ok and res.node.text == "27,430.09"


def test_descriptor_description_never_quotes_record_content():
    """This string is written into a committed artifact and into error logs."""
    d = describe_control(ACCOUNTS.node("t3"), ACCOUNTS)
    assert "4,182.55" not in d.description
    assert "Current Balance" in d.description


def test_resolution_falls_back_and_reports_it_as_drift():
    obs = Observation(step=0, controls=[
        field("e1", "Member ID", name="ctl00$txtMemberId")])
    d = describe_control(obs.controls[0], obs)
    # The vendor renamed the label; the generated control name survived.
    renamed = Observation(step=0, controls=[
        field("e1", "Member Number", name="ctl00$txtMemberId")])
    res = resolve(d, renamed)
    assert res.ok, "fallback should still find the control"
    assert res.used_fallback, "and should announce that it needed a fallback"
    assert res.strategy.kind is LocatorKind.DOM_ATTR


def test_ambiguity_is_refused_rather_than_guessed():
    d = ControlDescriptor(description="a balance", strategies=[
        LocatorStrategy(kind=LocatorKind.AX_ROLE_NAME, rank=0, role="cell")])
    res = resolve(d, ACCOUNTS)
    assert not res.ok and res.ambiguous
    assert any("ambiguous" in a for a in res.attempts)


def test_unresolvable_control_explains_every_attempt():
    d = ControlDescriptor(description="missing", strategies=[
        LocatorStrategy(kind=LocatorKind.LABEL_TEXT, rank=0, role="textbox",
                        label=NameMatch(value="Nope")),
        LocatorStrategy(kind=LocatorKind.DOM_ATTR, rank=1, attr="name", attr_value="x"),
    ])
    res = resolve(d, ACCOUNTS)
    assert not res.ok
    assert len(res.attempts) == 2 and all("no match" in a for a in res.attempts)


def test_frame_is_a_preference_not_a_filter():
    """A control that moved between frames is still the right control."""
    node = field("e1", "Member ID")
    node.frame_path = ["mcbmain"]
    obs = Observation(step=0, controls=[node])
    d = describe_control(node, obs)
    moved = field("e1", "Member ID")
    moved.frame_path = ["contentFrame"]
    res = resolve(d, Observation(step=0, controls=[moved]))
    assert res.ok and "any frame" in " ".join(res.attempts)


def test_ordinal_is_recorded_only_when_nothing_else_identifies_the_control():
    bare = UINode(handle="e9", role="button")
    obs = Observation(step=0, controls=[bare, UINode(handle="e8", role="button")])
    d = describe_control(bare, obs)
    assert d.primary.kind is LocatorKind.ORDINAL
    assert d.primary.confidence <= 0.25, "positional targeting must be flagged low-confidence"
