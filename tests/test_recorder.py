"""Recorder decisions that make the difference between a capability that works
once and one that works for every caller."""
from __future__ import annotations

from ledgerhand.agent.recorder import _derive_checkpoint, surface_fingerprint
from ledgerhand.models.observation import Observation, UINode


def obs(texts, *, title="", controls=()):
    return Observation(step=0, title=title, texts=list(texts), controls=list(controls))


BEFORE = obs(["Member Inquiry", "Member ID"])
DETAIL = obs(
    ["Home > Member Inquiry > Detail", "Member Detail", "Member",
     "DANA WHITFIELD (12345)", "Branch", "MAIN", "Status", "ACTIVE",
     "Account", "Type", "Current Balance", "4,182.55"],
    title="First Valley Credit Union - Member Detail",
)


def test_checkpoint_picks_screen_chrome_not_record_content():
    """`MAIN` is member 12345's branch. Asserting on it would give a capability
    that passes its own replay and fails for the next member."""
    condition = _derive_checkpoint(BEFORE, DETAIL, {"12345"})
    assert condition.text == "Member Detail"


def test_checkpoint_never_uses_a_parameter_value():
    tainted = obs(["Whitfield Holdings", "Account Summary"])
    condition = _derive_checkpoint(BEFORE, tainted, {"Whitfield Holdings"})
    assert condition is None or "Whitfield" not in condition.text


def test_checkpoint_rejects_anything_containing_a_digit():
    """Ids, balances, dates and confirmation numbers all vary per invocation."""
    numeric = obs(["Confirmation 84213", "Posted 2026-01-04"])
    assert _derive_checkpoint(BEFORE, numeric, set()) is None


def test_checkpoint_rejects_a_value_rendered_by_a_control():
    screen = obs(["Account Summary", "PLATINUM"],
                 controls=[UINode(handle="e1", role="textbox", value="PLATINUM")])
    condition = _derive_checkpoint(BEFORE, screen, set())
    assert condition is not None and condition.text == "Account Summary"


def test_checkpoint_declines_rather_than_asserting_on_something_unsafe():
    """Saying 'no safe checkpoint here' beats writing one that fails later."""
    only_data = obs(["DANA WHITFIELD", "MAIN", "ACTIVE"])
    assert _derive_checkpoint(BEFORE, only_data, set()) is None


def test_confirmation_screens_are_checkpointable():
    confirmation = obs(
        ["Home > Detail > Confirmation", "Sub-account opened successfully.",
         "Confirmation", "New Account Number", "0004-4548"],
        title="First Valley Credit Union - Confirmation")
    condition = _derive_checkpoint(DETAIL, confirmation, {"23456"})
    assert condition is not None
    assert "0004-4548" not in condition.text


def test_fingerprint_tracks_labels_not_content():
    """It should notice a vendor renaming a field, not a different member."""
    a = obs([], controls=[UINode(handle="e1", role="textbox", inferred_label="Member ID")])
    b = obs([], controls=[UINode(handle="e1", role="textbox", inferred_label="Member ID",
                                 value="99999")])
    c = obs([], controls=[UINode(handle="e1", role="textbox", inferred_label="Member Number")])
    assert surface_fingerprint(a) == surface_fingerprint(b)
    assert surface_fingerprint(a) != surface_fingerprint(c)


def test_fingerprint_ignores_content_cells():
    """A cell reports its own text as its label. Hashing that made the
    fingerprint change for every member and reported drift on a capability
    that was working perfectly."""
    def screen(member):
        return obs([], controls=[
            UINode(handle="e1", role="textbox", inferred_label="Member ID"),
            UINode(handle="t1", role="cell", text=member),
        ])
    assert surface_fingerprint(screen("DANA WHITFIELD")) == \
        surface_fingerprint(screen("MARCUS OYELARAN"))
