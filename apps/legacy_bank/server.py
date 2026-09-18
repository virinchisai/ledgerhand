"""
Meridian Core 7.x -- Member Servicing Console (MOCK).

A deliberately *legacy* back-office web app used as the automation target.
It is a stand-in for the class of application described in the brief: no API,
server-rendered HTML, table-based layout, cryptic generated control names,
content inside a nested frame, and no test IDs anywhere.

Two tenants run the same underlying vendor product with different branding,
labels, routes and versions -- this is what makes cross-tenant reuse testable.

It also exposes an /admin/chaos endpoint so runtime conditions (not-found,
validation errors, permission denials, interstitials, session expiry, slow
loads, server errors) can be injected deliberately rather than waited for.

No real data, no real credentials, no real institution.
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

# --------------------------------------------------------------------------
# Tenant configuration: same vendor product, different skin/labels/routes.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Tenant:
    slug: str
    display: str
    product_version: str
    # Label drift is the realistic cross-tenant problem: same control,
    # different words on it.
    member_id_label: str
    search_button: str
    search_route: str
    heading: str
    accent: str


TENANTS: dict[str, Tenant] = {
    "firstvalley": Tenant(
        slug="firstvalley",
        display="First Valley Credit Union",
        product_version="7.2.14",
        member_id_label="Member ID",
        search_button="Search",
        search_route="search",
        heading="Member Servicing",
        accent="#1f3864",
    ),
    "summitcu": Tenant(
        slug="summitcu",
        display="Summit Community CU",
        product_version="7.4.03",
        member_id_label="Member Number",
        search_button="Find",
        search_route="lookup",
        heading="Member Services",
        accent="#5b2333",
    ),
}

# --------------------------------------------------------------------------
# Seed data. Entirely synthetic.
# --------------------------------------------------------------------------


@dataclass
class Account:
    number: str
    kind: str
    balance: float
    status: str = "ACTIVE"


@dataclass
class Member:
    member_id: str
    name: str
    branch: str
    status: str = "ACTIVE"
    restricted: bool = False
    accounts: list[Account] = field(default_factory=list)


def _seed() -> dict[str, Member]:
    return {
        "12345": Member(
            "12345", "DANA WHITFIELD", "MAIN",
            accounts=[
                Account("0001-4471", "SAVINGS", 4182.55),
                Account("0002-1180", "CHECKING", 912.10),
                Account("0003-7702", "MONEY MARKET", 15000.00),
            ],
        ),
        "23456": Member(
            "23456", "MARCUS OYELARAN", "WESTGATE",
            accounts=[
                Account("0001-9930", "SAVINGS", 27430.09),
                Account("0002-5512", "CHECKING", 3311.87),
            ],
        ),
        "55555": Member(
            "55555", "RESTRICTED ACCOUNT", "MAIN",
            status="RESTRICTED", restricted=True,
            accounts=[Account("0001-0000", "SAVINGS", 0.0)],
        ),
    }


MEMBERS: dict[str, Member] = _seed()
SESSIONS: dict[str, dict] = {}

# Injected runtime conditions. Each is consumed on next matching request
# unless 'sticky' is set.
CHAOS: dict[str, object] = {
    "interstitial": False,
    "slow_ms": 0,
    "expire_session": False,
    "server_error": False,
    "force_validation": False,
}

DEMO_OPERATOR = "svc.automation"
DEMO_PASSWORD = "Sandbox!Demo1"  # mock target credential; never leaves the box

app = FastAPI(title="Meridian Core (mock)", docs_url=None, redoc_url=None)


# --------------------------------------------------------------------------
# Legacy chrome helpers -- intentionally non-semantic markup.
# --------------------------------------------------------------------------

def _shell(t: Tenant, title: str, body: str, *, crumb: str = "") -> str:
    """Wrap content in period-typical markup: tables, <font>, bgcolor, no IDs."""
    return f"""<html><head><title>{t.display} - {title}</title>
<style>body{{font-family:Verdana,Arial;font-size:11px;margin:0;background:#f4f4f0}}
td{{font-size:11px}} .hdr{{background:{t.accent};color:#fff;padding:6px}}
.err{{background:#ffe8e8;border:1px solid #cc0000;color:#900;padding:6px;margin:6px 0}}
.warn{{background:#fff8dc;border:1px solid #d4a017;padding:6px;margin:6px 0}}
.ok{{background:#eaffea;border:1px solid #2d862d;padding:6px;margin:6px 0}}
</style></head>
<body>
<table width="100%" cellpadding="0" cellspacing="0"><tr><td class="hdr">
<font size="2"><b>{t.display}</b></font> &nbsp;|&nbsp;
<font size="1">Meridian Core {t.product_version} &mdash; {t.heading}</font>
</td></tr></table>
<table width="100%" cellpadding="4"><tr><td>
<font size="1" color="#666">{crumb}</font>
{body}
</td></tr></table>
</body></html>"""


def _interstitial_block(t: Tenant, return_to: str) -> str:
    """An unexpected modal that legitimately appears at runtime."""
    return f"""<div class="warn">
<table cellpadding="3"><tr><td>
<font size="2"><b>System Maintenance Notice</b></font><br>
<font size="1">A scheduled maintenance window begins at 23:00 ET. Unsaved work
will be lost. Acknowledge to continue.</font><br><br>
<form method="get" action="{return_to}">
<input type="hidden" name="ack" value="1">
<input type="submit" name="ctl00$Notice$btnAck" value="Acknowledge">
</form>
</td></tr></table></div>"""


def _sid(request: Request) -> str | None:
    sid = request.cookies.get("MCBSESS")
    if sid and sid in SESSIONS:
        return sid
    return None


def _maybe_chaos(kind: str) -> bool:
    """Consume a one-shot injected condition."""
    if CHAOS.get(kind):
        CHAOS[kind] = False
        return True
    return False


def _delay() -> None:
    ms = int(CHAOS.get("slow_ms") or 0)
    if ms:
        time.sleep(ms / 1000.0)


def _session_gate(request: Request, t: Tenant) -> HTMLResponse | None:
    """Return a login-required page if the session is gone/expired."""
    if _maybe_chaos("expire_session"):
        sid = request.cookies.get("MCBSESS")
        SESSIONS.pop(sid, None)
    if _sid(request) is None:
        return HTMLResponse(
            _shell(t, "Session Expired", f"""
<div class="err"><font size="2"><b>Your session has expired.</b></font><br>
<font size="1">For security, sessions end after a period of inactivity.
Please sign in again.</font></div>
<a href="/t/{t.slug}/">Return to sign-on</a>"""),
            status_code=200,
        )
    return None


def _t(slug: str) -> Tenant:
    return TENANTS.get(slug) or TENANTS["firstvalley"]


# --------------------------------------------------------------------------
# Admin / chaos control (not part of the automated surface)
# --------------------------------------------------------------------------

@app.get("/admin/health")
def health() -> JSONResponse:
    return JSONResponse({"ok": True, "tenants": list(TENANTS)})


@app.post("/admin/chaos")
async def set_chaos(request: Request) -> JSONResponse:
    payload = await request.json()
    for k, v in payload.items():
        if k in CHAOS:
            CHAOS[k] = v
    return JSONResponse({"chaos": CHAOS})


@app.post("/admin/reset")
def reset() -> JSONResponse:
    global MEMBERS
    MEMBERS = _seed()
    SESSIONS.clear()
    for k in CHAOS:
        CHAOS[k] = 0 if k == "slow_ms" else False
    return JSONResponse({"reset": True})


# --------------------------------------------------------------------------
# Sign-on
# --------------------------------------------------------------------------

@app.get("/t/{slug}/", response_class=HTMLResponse)
@app.get("/t/{slug}", response_class=HTMLResponse)
def logon_page(slug: str, bad: int = 0) -> HTMLResponse:
    t = _t(slug)
    err = '<div class="err">Invalid operator ID or password.</div>' if bad else ""
    # Note: no <label for>, no ids -- labels are plain table cells.
    body = f"""{err}
<form method="post" action="/t/{t.slug}/logon">
<table cellpadding="6" cellspacing="0" border="0" width="420">
<tr><td colspan="2"><font size="3"><b>Operator Sign-On</b></font></td></tr>
<tr><td width="140"><font size="2">Operator ID</font></td>
    <td><input type="text" name="ctl00$Logon$txtOperator" size="24"></td></tr>
<tr><td><font size="2">Password</font></td>
    <td><input type="password" name="ctl00$Logon$txtPassword" size="24"></td></tr>
<tr><td></td><td><input type="submit" name="ctl00$Logon$btnSignOn" value="Sign On"></td></tr>
</table>
</form>
<font size="1" color="#888">Authorized use only. Activity is monitored.</font>"""
    return HTMLResponse(_shell(t, "Sign-On", body, crumb="Sign-On"))


@app.post("/t/{slug}/logon")
def logon(
    slug: str,
    ctl00_Logon_txtOperator: str = Form("", alias="ctl00$Logon$txtOperator"),
    ctl00_Logon_txtPassword: str = Form("", alias="ctl00$Logon$txtPassword"),
) -> RedirectResponse:
    t = _t(slug)
    if ctl00_Logon_txtOperator.strip() != DEMO_OPERATOR or ctl00_Logon_txtPassword != DEMO_PASSWORD:
        return RedirectResponse(f"/t/{t.slug}/?bad=1", status_code=303)
    sid = uuid.uuid4().hex
    SESSIONS[sid] = {"operator": ctl00_Logon_txtOperator, "tenant": t.slug, "at": time.time()}
    resp = RedirectResponse(f"/t/{t.slug}/console", status_code=303)
    resp.set_cookie("MCBSESS", sid, httponly=True, samesite="lax")
    return resp


# --------------------------------------------------------------------------
# Console shell: content lives in a nested frame (frame traversal required)
# --------------------------------------------------------------------------

@app.get("/t/{slug}/console", response_class=HTMLResponse)
def console(slug: str, request: Request) -> HTMLResponse:
    t = _t(slug)
    if (gate := _session_gate(request, t)) is not None:
        return gate
    sr = t.search_route
    body = f"""<table width="100%" cellpadding="0" cellspacing="0" border="0"><tr>
<td width="170" valign="top" bgcolor="#e8e8e0">
  <table cellpadding="4"><tr><td><font size="2"><b>{t.heading}</b></font></td></tr>
  <tr><td><a href="/t/{t.slug}/members/{sr}" target="mcbmain"><font size="2">Member Inquiry</font></a></td></tr>
  <tr><td><a href="#" onclick="return false"><font size="2" color="#999">Teller Ops</font></a></td></tr>
  <tr><td><a href="#" onclick="return false"><font size="2" color="#999">GL Inquiry</font></a></td></tr>
  </table>
</td>
<td valign="top">
  <iframe name="mcbmain" src="/t/{t.slug}/members/{sr}" width="100%" height="460"
          frameborder="0" scrolling="auto"></iframe>
</td></tr></table>"""
    return HTMLResponse(_shell(t, "Console", body, crumb="Home"))


# --------------------------------------------------------------------------
# Member search  (route name differs per tenant: /search vs /lookup)
# --------------------------------------------------------------------------

def _search_form(t: Tenant, *, error: str = "", ack: bool = False) -> str:
    inter = ""
    if not ack and _maybe_chaos("interstitial"):
        inter = _interstitial_block(t, f"/t/{t.slug}/members/{t.search_route}")
    err = f'<div class="err"><font size="2">{error}</font></div>' if error else ""
    # Tenant B carries an extra field -- benign per-tenant drift.
    extra = ""
    if t.slug == "summitcu":
        extra = """<tr><td><font size="2">Region</font></td>
        <td><select name="ctl00$MainContent$ddlRegion">
        <option value="">(all)</option><option>NORTH</option><option>SOUTH</option>
        </select></td></tr>"""
    return f"""{inter}{err}
<form method="post" action="/t/{t.slug}/members/{t.search_route}">
<table cellpadding="5" cellspacing="0" border="0">
<tr><td colspan="2"><font size="3"><b>Member Inquiry</b></font></td></tr>
<tr><td width="130"><font size="2">{t.member_id_label}</font></td>
    <td><input type="text" name="ctl00$MainContent$txtMemberId" size="18" maxlength="12"></td></tr>
{extra}
<tr><td></td><td>
  <input type="submit" name="ctl00$MainContent$btnSearch" value="{t.search_button}">
  &nbsp;<input type="reset" value="Clear">
</td></tr>
</table>
</form>
<br><font size="1" color="#888">Enter the {t.member_id_label.lower()} to retrieve servicing detail.</font>"""


@app.get("/t/{slug}/members/{route}", response_class=HTMLResponse)
def search_page(slug: str, route: str, request: Request, ack: int = 0) -> HTMLResponse:
    t = _t(slug)
    if route != t.search_route:
        return HTMLResponse(_shell(t, "Not Found", '<div class="err">No such page.</div>'), status_code=404)
    if (gate := _session_gate(request, t)) is not None:
        return gate
    _delay()
    if _maybe_chaos("server_error"):
        return HTMLResponse(
            _shell(t, "Error", '<div class="err"><b>Unexpected system error.</b><br>'
                               'Reference MCB-500-8831. Contact the service desk.</div>'),
            status_code=500,
        )
    return HTMLResponse(_shell(t, "Member Inquiry", _search_form(t, ack=bool(ack)), crumb="Home &gt; Member Inquiry"))


@app.post("/t/{slug}/members/{route}", response_class=HTMLResponse)
def search_submit(
    slug: str,
    route: str,
    request: Request,
    member_id: str = Form("", alias="ctl00$MainContent$txtMemberId"),
) -> HTMLResponse:
    t = _t(slug)
    if (gate := _session_gate(request, t)) is not None:
        return gate
    _delay()
    mid = member_id.strip()
    if not mid:
        return HTMLResponse(_shell(t, "Member Inquiry",
                                   _search_form(t, error=f"{t.member_id_label} is required.", ack=True)))
    if not mid.isdigit():
        return HTMLResponse(_shell(t, "Member Inquiry",
                                   _search_form(t, error=f"{t.member_id_label} must be numeric.", ack=True)))
    member = MEMBERS.get(mid)
    if member is None:
        # A legitimate business outcome, not an application failure.
        return HTMLResponse(_shell(t, "Member Inquiry", f"""
<div class="err"><font size="2"><b>No member found for {t.member_id_label.lower()} {mid}.</b><br>
Verify the number and try again. (MCB-0042)</font></div>
{_search_form(t, ack=True)}""", crumb="Home &gt; Member Inquiry"))
    return RedirectResponse(f"/t/{t.slug}/members/detail/{mid}", status_code=303)


# --------------------------------------------------------------------------
# Member detail -- balances live in a nested, non-semantic table
# --------------------------------------------------------------------------

@app.get("/t/{slug}/members/detail/{mid}", response_class=HTMLResponse)
def member_detail(slug: str, mid: str, request: Request) -> HTMLResponse:
    t = _t(slug)
    if (gate := _session_gate(request, t)) is not None:
        return gate
    _delay()
    member = MEMBERS.get(mid)
    if member is None:
        return HTMLResponse(_shell(t, "Member Inquiry", f"""
<div class="err"><font size="2"><b>No member found for {t.member_id_label.lower()} {mid}.</b> (MCB-0042)</font></div>
{_search_form(t, ack=True)}"""), status_code=200)
    if member.restricted:
        # Permission denial -- also a business outcome the caller must know about.
        return HTMLResponse(_shell(t, "Access Denied", f"""
<div class="err"><font size="2"><b>Access denied.</b><br>
This member record is restricted. Operator role SERVICING is not permitted to
view account detail. (MCB-0310)</font></div>
<a href="/t/{t.slug}/members/{t.search_route}">Back to Member Inquiry</a>""",
                                   crumb="Home &gt; Member Inquiry &gt; Restricted"))
    rows = ""
    for a in member.accounts:
        rows += f"""<tr>
<td><font size="2">{a.number}</font></td>
<td><font size="2">{a.kind}</font></td>
<td><font size="2">{a.status}</font></td>
<td align="right"><font size="2">{a.balance:,.2f}</font></td></tr>"""
    body = f"""
<table cellpadding="4" cellspacing="0" border="0" width="100%">
<tr><td colspan="4"><font size="3"><b>Member Detail</b></font></td></tr>
<tr><td width="120"><font size="2">Member</font></td>
    <td colspan="3"><font size="2"><b>{member.name}</b> &nbsp; ({member.member_id})</font></td></tr>
<tr><td><font size="2">Branch</font></td><td colspan="3"><font size="2">{member.branch}</font></td></tr>
<tr><td><font size="2">Status</font></td><td colspan="3"><font size="2">{member.status}</font></td></tr>
</table>
<br>
<table cellpadding="3" cellspacing="0" border="1" bordercolor="#cccccc" width="100%">
<tr bgcolor="#dddddd">
  <td><font size="2"><b>Account</b></font></td>
  <td><font size="2"><b>Type</b></font></td>
  <td><font size="2"><b>Status</b></font></td>
  <td align="right"><font size="2"><b>Current Balance</b></font></td>
</tr>
{rows}
</table>
<br>
<a href="/t/{t.slug}/members/detail/{mid}/subacct"><font size="2">Open Sub-Account</font></a>
&nbsp;|&nbsp;
<a href="/t/{t.slug}/members/{t.search_route}"><font size="2">New Search</font></a>"""
    return HTMLResponse(_shell(t, "Member Detail", body,
                               crumb="Home &gt; Member Inquiry &gt; Detail"))


# --------------------------------------------------------------------------
# Sub-account opening -- multi-field form + validation + confirmation
# --------------------------------------------------------------------------

def _subacct_form(t: Tenant, mid: str, *, error: str = "") -> str:
    err = f'<div class="err"><font size="2">{error}</font></div>' if error else ""
    return f"""{err}
<form method="post" action="/t/{t.slug}/members/detail/{mid}/subacct">
<table cellpadding="5" cellspacing="0" border="0">
<tr><td colspan="2"><font size="3"><b>Open Sub-Account</b></font></td></tr>
<tr><td colspan="2"><font size="1" color="#666">Member {mid}</font></td></tr>
<tr><td width="150"><font size="2">Account Type</font></td>
    <td><select name="ctl00$MainContent$ddlAcctType">
      <option value="">-- select --</option>
      <option value="SAV">SAVINGS</option>
      <option value="CHK">CHECKING</option>
      <option value="MMK">MONEY MARKET</option>
    </select></td></tr>
<tr><td><font size="2">Nickname</font></td>
    <td><input type="text" name="ctl00$MainContent$txtNickname" size="24" maxlength="20"></td></tr>
<tr><td><font size="2">Initial Deposit</font></td>
    <td><input type="text" name="ctl00$MainContent$txtDeposit" size="12"> <font size="1">(min 25.00)</font></td></tr>
<tr><td></td><td><input type="submit" name="ctl00$MainContent$btnOpen" value="Open Account"></td></tr>
</table>
</form>"""


@app.get("/t/{slug}/members/detail/{mid}/subacct", response_class=HTMLResponse)
def subacct_page(slug: str, mid: str, request: Request) -> HTMLResponse:
    t = _t(slug)
    if (gate := _session_gate(request, t)) is not None:
        return gate
    _delay()
    if mid not in MEMBERS:
        return HTMLResponse(_shell(t, "Not Found",
                                   f'<div class="err">No member found for {mid}. (MCB-0042)</div>'))
    return HTMLResponse(_shell(t, "Open Sub-Account", _subacct_form(t, mid),
                               crumb="Home &gt; Member Inquiry &gt; Detail &gt; Open Sub-Account"))


@app.post("/t/{slug}/members/detail/{mid}/subacct", response_class=HTMLResponse)
def subacct_submit(
    slug: str,
    mid: str,
    request: Request,
    acct_type: str = Form("", alias="ctl00$MainContent$ddlAcctType"),
    nickname: str = Form("", alias="ctl00$MainContent$txtNickname"),
    deposit: str = Form("", alias="ctl00$MainContent$txtDeposit"),
) -> HTMLResponse:
    t = _t(slug)
    if (gate := _session_gate(request, t)) is not None:
        return gate
    _delay()
    member = MEMBERS.get(mid)
    if member is None:
        return HTMLResponse(_shell(t, "Not Found",
                                   f'<div class="err">No member found for {mid}. (MCB-0042)</div>'))
    if _maybe_chaos("force_validation"):
        return HTMLResponse(_shell(t, "Open Sub-Account",
                                   _subacct_form(t, mid, error="Deposit posting is temporarily unavailable. (MCB-0199)")))
    if not acct_type:
        return HTMLResponse(_shell(t, "Open Sub-Account",
                                   _subacct_form(t, mid, error="Account Type is required. (MCB-0101)")))
    try:
        amt = float((deposit or "").replace(",", "").replace("$", "").strip())
    except ValueError:
        return HTMLResponse(_shell(t, "Open Sub-Account",
                                   _subacct_form(t, mid, error="Initial Deposit must be a number. (MCB-0102)")))
    if amt < 25.0:
        return HTMLResponse(_shell(t, "Open Sub-Account",
                                   _subacct_form(t, mid, error="Initial Deposit must be at least 25.00. (MCB-0103)")))

    kind = {"SAV": "SAVINGS", "CHK": "CHECKING", "MMK": "MONEY MARKET"}[acct_type]
    seq = len(member.accounts) + 1
    number = f"000{seq}-{4000 + seq * 137}"
    member.accounts.append(Account(number, kind, amt))
    body = f"""
<div class="ok"><font size="2"><b>Sub-account opened successfully.</b></font></div>
<table cellpadding="4" cellspacing="0" border="0">
<tr><td colspan="2"><font size="3"><b>Confirmation</b></font></td></tr>
<tr><td width="150"><font size="2">New Account Number</font></td>
    <td><font size="2"><b>{number}</b></font></td></tr>
<tr><td><font size="2">Account Type</font></td><td><font size="2">{kind}</font></td></tr>
<tr><td><font size="2">Nickname</font></td><td><font size="2">{nickname or "(none)"}</font></td></tr>
<tr><td><font size="2">Opening Balance</font></td><td><font size="2">{amt:,.2f}</font></td></tr>
<tr><td><font size="2">Confirmation No.</font></td>
    <td><font size="2">CNF-{uuid.uuid4().hex[:8].upper()}</font></td></tr>
</table>
<br><a href="/t/{t.slug}/members/detail/{mid}"><font size="2">Return to Member Detail</font></a>"""
    return HTMLResponse(_shell(t, "Confirmation", body,
                               crumb="Home &gt; Member Inquiry &gt; Detail &gt; Confirmation"))


def main() -> None:
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8848, log_level="warning")


if __name__ == "__main__":
    main()
