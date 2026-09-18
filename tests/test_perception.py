"""Perception against the real legacy markup.

These exist because every claim the design makes about "works without a clean
DOM" is a claim about this layer. They drive a real browser against the real
target app rather than a fixture, because the whole point is what the markup
actually does.
"""
from __future__ import annotations

import pytest

from ledgerhand.models.enums import ActionKind
from ledgerhand.surface.base import ActRequest
from ledgerhand.surface.web import WebSurface

BASE = "http://127.0.0.1:8848"


@pytest.fixture(scope="module")
def surface():
    s = WebSurface(headless=True)
    s.start()
    yield s
    s.close()


def signed_on(surface) -> None:
    surface.navigate(f"{BASE}/t/firstvalley/")
    surface.settle()
    obs = surface.perceive(0)
    pick = lambda t: next(n for n in obs.controls if t in n.label)
    surface.act(ActRequest(ActionKind.TYPE, node=pick("Operator ID"), value="svc.automation"))
    surface.act(ActRequest(ActionKind.TYPE, node=pick("Password"),
                           value="Sandbox!Demo1", sensitive=True))
    surface.act(ActRequest(ActionKind.CLICK, node=next(n for n in obs.controls if n.role == "button")))
    surface.settle()


def test_label_is_recovered_when_the_browser_computes_no_accessible_name(app_server, surface):
    """The premise of the whole perception layer.

    These inputs have no <label for>, no aria-label and no placeholder -- the
    label is a sibling <td>. Chrome's own accessibility tree reports an empty
    name for them, which is why perception is computed in-page.
    """
    surface.navigate(f"{BASE}/t/firstvalley/")
    surface.settle()
    obs = surface.perceive(0)
    boxes = [n for n in obs.controls if n.role == "textbox"]
    assert len(boxes) == 2
    assert {n.label for n in boxes} == {"Operator ID", "Password"}
    # Recovered from layout, not from an accessible name.
    assert all(n.name == "" for n in boxes)
    assert all(n.inferred_label for n in boxes)


def test_generated_control_names_are_captured_as_a_fallback(app_server, surface):
    surface.navigate(f"{BASE}/t/firstvalley/")
    surface.settle()
    obs = surface.perceive(0)
    names = {n.attrs.get("name") for n in obs.controls}
    assert "ctl00$Logon$txtOperator" in names


def test_password_values_are_never_perceived(app_server, surface):
    """Perception must not read a password field back out of the page."""
    surface.navigate(f"{BASE}/t/firstvalley/")
    surface.settle()
    obs = surface.perceive(0)
    password = next(n for n in obs.controls if n.label == "Password")
    surface.act(ActRequest(ActionKind.TYPE, node=password, value="Sandbox!Demo1", sensitive=True))
    again = surface.perceive(1)
    assert all("Sandbox!Demo1" not in (n.value or "") for n in again.controls)


def test_controls_inside_a_nested_frame_are_perceived_with_their_path(app_server, surface):
    signed_on(surface)
    obs = surface.perceive(2)
    assert "mcbmain" in obs.frames
    framed = [n for n in obs.controls if n.frame_path == ["mcbmain"]]
    assert any(n.label == "Member ID" for n in framed)
    assert any(n.role == "button" and n.label == "Search" for n in framed)


def test_frame_geometry_is_absolute_so_real_clicks_land(app_server, surface):
    """Coordinates are offset by the frame's own position in the page."""
    signed_on(surface)
    obs = surface.perceive(2)
    member_id = next(n for n in obs.controls if n.label == "Member ID")
    assert member_id.box is not None
    x, y, w, h = member_id.box
    assert w > 0 and h > 0
    assert x > 0 and y > 0


def test_table_cells_carry_the_context_needed_to_address_them(app_server, surface):
    signed_on(surface)
    obs = surface.perceive(2)
    member_id = next(n for n in obs.controls if n.label == "Member ID")
    surface.act(ActRequest(ActionKind.TYPE, node=member_id, value="12345"))
    surface.act(ActRequest(ActionKind.CLICK,
                           node=next(n for n in obs.controls
                                     if n.role == "button" and n.label == "Search")))
    surface.settle()
    detail = surface.perceive(3)
    balance = next(n for n in detail.controls
                   if n.text == "4,182.55" and n.attrs.get("col_header"))
    assert balance.attrs["col_header"] == "Current Balance"
    assert "SAVINGS" in balance.attrs["row_text"]


def test_layout_tables_do_not_get_invented_column_headers(app_server, surface):
    """A column header only means something in a grid. Inventing one for a
    two-column layout table produces anchors that look precise and are not."""
    signed_on(surface)
    obs = surface.perceive(2)
    nav = [n for n in obs.controls if n.role == "link" and "Teller" in n.text]
    assert nav, "expected the nav links"
    assert all(not n.attrs.get("col_header") for n in nav)


def test_a_node_cannot_be_acted_on_after_the_page_moves(app_server, surface):
    """Handles are valid only for the observation they came from."""
    surface.navigate(f"{BASE}/t/firstvalley/")
    surface.settle()
    stale = next(n for n in surface.perceive(0).controls if n.role == "button")
    surface.navigate(f"{BASE}/t/summitcu/")
    surface.settle()
    result = surface.act(ActRequest(ActionKind.CLICK, node=stale))
    assert not result.ok and "no longer present" in (result.error or "")


def test_http_500_is_reported_as_a_transport_error(app_server, surface, chaos):
    signed_on(surface)
    chaos(server_error=True)
    surface.navigate(f"{BASE}/t/firstvalley/members/search")
    surface.settle()
    obs = surface.perceive(9)
    assert obs.transport_error and "500" in obs.transport_error
