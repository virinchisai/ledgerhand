"""Operator console -- a deliberately minimal surface over a real mechanism.

The brief allows mocking the console. What is mocked here is the *presentation*:
this is server-rendered HTML with a poll-refreshed screenshot, not a co-browsing
product. What is not mocked is everything underneath -- the intervention queue,
the claim, the lease transfer, the actions, the handback, and the evidence trail
are the same objects and the same code path a real console would drive.

Every surface interaction is submitted to the automation thread through the
broker rather than performed here, because this server runs on a different
thread from the one that owns the browser. See broker.SessionBroker.submit.
"""
from __future__ import annotations

import html
import threading
import time
from typing import Any

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from ..models.enums import ActionKind, ControlOwner
from ..models.intervention import InterventionRequest, InterventionStatus
from .broker import ControlError, SessionBroker

_STYLE = """<style>
body{font:13px/1.5 -apple-system,Segoe UI,Roboto,sans-serif;margin:0;background:#11131a;color:#e7e9ee}
a{color:#7aa7ff} .wrap{max-width:1100px;margin:0 auto;padding:18px}
.card{background:#1a1d27;border:1px solid #2a2f3d;border-radius:8px;padding:14px;margin-bottom:14px}
.row{display:flex;gap:16px;flex-wrap:wrap} .col{flex:1;min-width:320px}
h1{font-size:18px;margin:0 0 12px} h2{font-size:14px;margin:0 0 8px;color:#9aa3b8}
.badge{display:inline-block;padding:2px 8px;border-radius:99px;font-size:11px;background:#2a2f3d}
.b-pending{background:#5a3a12;color:#ffd08a} .b-operator_active{background:#123a2a;color:#8affc4}
.b-returned{background:#1b2a4a;color:#9ec2ff} .b-resolved{background:#26292f;color:#9aa3b8}
pre{background:#0d0f15;border:1px solid #232838;padding:10px;border-radius:6px;overflow:auto;max-height:280px;font-size:11px}
img{max-width:100%;border:1px solid #2a2f3d;border-radius:6px;background:#fff}
input,select,button{font:12px inherit;background:#0d0f15;color:#e7e9ee;border:1px solid #333a4d;border-radius:5px;padding:6px 8px}
button{background:#2b5fd9;border-color:#2b5fd9;cursor:pointer} button.ghost{background:#1a1d27}
table{width:100%;border-collapse:collapse} td,th{text-align:left;padding:5px 6px;border-bottom:1px solid #232838;font-size:12px}
.muted{color:#8e97ab}
</style>"""


def _badge(status: InterventionStatus) -> str:
    return f'<span class="badge b-{status.value}">{status.value.replace("_", " ")}</span>'


def build_console(broker: SessionBroker) -> FastAPI:
    app = FastAPI(title="Ledgerhand operator console", docs_url=None, redoc_url=None)

    def page(body: str, *, refresh: int = 0) -> HTMLResponse:
        meta = f'<meta http-equiv="refresh" content="{refresh}">' if refresh else ""
        return HTMLResponse(
            f"<html><head><title>Operator console</title>{meta}{_STYLE}</head>"
            f"<body><div class='wrap'>{body}</div></body></html>")

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        rows = ""
        for req in sorted(broker.interventions.values(), key=lambda r: r.created_at, reverse=True):
            rows += (f"<tr><td><a href='/i/{req.id}'>{req.id}</a></td>"
                     f"<td>{_badge(req.status)}</td>"
                     f"<td>{html.escape(req.trigger.value)}</td>"
                     f"<td>{html.escape(req.capability or req.goal or '')[:60]}</td>"
                     f"<td class='muted'>{html.escape(req.reason)[:70]}</td></tr>")
        if not rows:
            rows = "<tr><td colspan='5' class='muted'>No interventions raised.</td></tr>"
        return page(f"""
<h1>Operator console</h1>
<div class="card">
  <h2>Session {html.escape(broker.session_id)}</h2>
  Control is held by <b>{broker.lease.owner.value}</b>
  ({html.escape(broker.lease.holder_label)})
</div>
<div class="card"><h2>Interventions</h2>
<table><tr><th>ID</th><th>Status</th><th>Trigger</th><th>Capability</th><th>Reason</th></tr>
{rows}</table></div>""", refresh=5)

    @app.get("/i/{intervention_id}", response_class=HTMLResponse)
    def detail(intervention_id: str) -> HTMLResponse:
        try:
            req = broker._get(intervention_id)
        except KeyError:
            return page("<h1>Unknown intervention</h1><a href='/'>back</a>")

        controls = ""
        if broker.lease.owner is ControlOwner.OPERATOR:
            try:
                obs = broker.submit(lambda: broker.surface.perceive(0), timeout=15)
                opts = "".join(
                    f"<option value='{html.escape(n.handle)}'>{html.escape(n.role)} "
                    f"&mdash; {html.escape((n.label or n.text)[:44])}</option>"
                    for n in obs.controls
                    if n.role in ("textbox", "button", "combobox", "link", "checkbox"))
                controls = f"""
<form method="post" action="/i/{req.id}/act" class="card">
  <h2>Act on the live session</h2>
  <select name="kind">
    <option value="click">click</option><option value="type">type</option>
    <option value="select">select</option><option value="press">press</option>
  </select>
  <select name="handle">{opts}</select>
  <input name="value" placeholder="value / key" size="18">
  <button type="submit">Send</button>
</form>
<form method="post" action="/i/{req.id}/handback" class="card">
  <h2>Hand control back</h2>
  <input name="resolution" placeholder="what you did" size="46">
  <button type="submit" name="resume" value="1">Resume automation</button>
  <button type="submit" name="resume" value="0" class="ghost">Abort run</button>
</form>"""
            except Exception as exc:  # the automation thread may have moved on
                controls = f"<div class='card muted'>live view unavailable: {html.escape(str(exc))}</div>"

        actions = "".join(
            f"<tr><td class='muted'>{a.at:%H:%M:%S}</td><td>{html.escape(a.kind)}</td>"
            f"<td>{html.escape(a.target)}</td><td class='muted'>{html.escape(a.note)[:60]}</td></tr>"
            for a in req.operator_actions) or "<tr><td colspan='4' class='muted'>none yet</td></tr>"

        buttons = ""
        if req.status is InterventionStatus.PENDING:
            buttons = (f"<form method='post' action='/i/{req.id}/claim'>"
                       f"<input name='operator' value='op.demo' size='14'> "
                       f"<button type='submit'>Claim</button></form>")
        elif req.status is InterventionStatus.CLAIMED:
            buttons = (f"<form method='post' action='/i/{req.id}/control'>"
                       f"<button type='submit'>Take control of live session</button></form>")

        return page(f"""
<a href="/">&larr; all interventions</a>
<h1>{html.escape(req.id)} {_badge(req.status)}</h1>
<div class="row">
  <div class="col">
    <div class="card">
      <h2>Why the automation stopped</h2>
      <b>{html.escape(req.trigger.value)}</b><br>
      <span class="muted">{html.escape(req.reason)}</span>
      <table style="margin-top:10px">
        <tr><th>Capability</th><td>{html.escape(req.capability or '-')}</td></tr>
        <tr><th>Goal</th><td>{html.escape(req.goal or '-')}</td></tr>
        <tr><th>Tenant</th><td>{html.escape(req.tenant)}</td></tr>
        <tr><th>Step</th><td>{req.step_index} &mdash; {html.escape(req.step_intent or '-')}</td></tr>
        <tr><th>URL</th><td class="muted">{html.escape(req.url)}</td></tr>
        <tr><th>Control</th><td>{broker.lease.owner.value} ({html.escape(broker.lease.holder_label)})</td></tr>
      </table>
      <p class="muted">{html.escape(req.requested_of_operator)}</p>
      {buttons}
    </div>
    {controls}
    <div class="card"><h2>What the operator did</h2>
      <table><tr><th>at</th><th>action</th><th>target</th><th>note</th></tr>{actions}</table>
    </div>
  </div>
  <div class="col">
    <div class="card"><h2>Live session</h2>
      <img src="/i/{req.id}/screen.png?t={req.status.value}" alt="live session">
    </div>
    <div class="card"><h2>State when it stopped</h2>
      <pre>{html.escape(req.observation_summary[:4000])}</pre>
    </div>
  </div>
</div>""", refresh=0 if broker.lease.owner is ControlOwner.OPERATOR else 5)

    @app.get("/i/{intervention_id}/screen.png")
    def screen(intervention_id: str) -> Response:
        """Live screenshot. Reads are open to the operator at any time -- they
        cannot damage anything, and seeing the session is the whole point."""
        import pathlib
        target = str(broker.evidence.dir / "screens" / f"console_{intervention_id}.png")
        try:
            path = broker.submit(lambda: broker.surface.screenshot(target), timeout=15)
            if path and pathlib.Path(path).exists():
                return Response(pathlib.Path(path).read_bytes(), media_type="image/png")
        except Exception:
            pass
        req = broker.interventions.get(intervention_id)
        if req and req.screenshot and pathlib.Path(req.screenshot).exists():
            return Response(pathlib.Path(req.screenshot).read_bytes(), media_type="image/png")
        return Response(b"", media_type="image/png", status_code=404)

    @app.post("/i/{intervention_id}/claim")
    def claim(intervention_id: str, operator: str = Form("op.demo")) -> RedirectResponse:
        broker.claim(intervention_id, operator)
        return RedirectResponse(f"/i/{intervention_id}", status_code=303)

    @app.post("/i/{intervention_id}/control")
    def control(intervention_id: str) -> RedirectResponse:
        broker.grant_control(intervention_id)
        return RedirectResponse(f"/i/{intervention_id}", status_code=303)

    @app.post("/i/{intervention_id}/act")
    def act(intervention_id: str, kind: str = Form(...), handle: str = Form(""),
            value: str = Form("")) -> RedirectResponse:
        token = broker.lease.token
        action = ActionKind(kind)
        try:
            broker.submit(lambda: broker.operator_act(
                intervention_id, token, kind=action,
                handle=handle or None,
                value=value or None,
                key=value or "Enter" if action is ActionKind.PRESS else None), timeout=45)
        except (ControlError, TimeoutError):
            pass
        return RedirectResponse(f"/i/{intervention_id}", status_code=303)

    @app.post("/i/{intervention_id}/handback")
    def handback(intervention_id: str, resume: str = Form("1"),
                 resolution: str = Form("")) -> RedirectResponse:
        try:
            broker.handback(intervention_id, broker.lease.token,
                            resume=resume == "1", resolution=resolution)
        except ControlError:
            pass
        return RedirectResponse(f"/i/{intervention_id}", status_code=303)

    return app


def serve_console(broker: SessionBroker, *, port: int = 8787) -> threading.Thread:
    """Run the console on a daemon thread alongside the automation."""
    import uvicorn

    app = build_console(broker)
    config = uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, name="operator-console", daemon=True)
    thread.start()
    return thread


class AutoOperator:
    """A scripted operator, for reproducible evidence.

    It drives the *same broker API* the HTML console posts to -- claim, take
    control, act, hand back -- and from a worker thread, submitting every
    surface-touching call through the broker exactly as the console does. That
    matters: the automation thread is parked in wait_for_handback pumping those
    commands, so an operator that acted inline would deadlock against the very
    mechanism it is meant to exercise.

    It stands in for the person, not for the mechanism.
    """

    def __init__(self, broker: SessionBroker, name: str = "auto.operator") -> None:
        self.broker = broker
        self.name = name
        self.error: Exception | None = None

    def run_async(
        self, intervention_id: str, steps: list[dict[str, Any]], *,
        resolution: str = "completed manually", resume: bool = True,
        delay_s: float = 0.2,
    ) -> threading.Thread:
        """Start the operator on its own thread and return it."""
        thread = threading.Thread(
            target=self._drive, name="auto-operator", daemon=True,
            args=(intervention_id, steps, resolution, resume, delay_s))
        thread.start()
        return thread

    def _drive(
        self, intervention_id: str, steps: list[dict[str, Any]],
        resolution: str, resume: bool, delay_s: float,
    ) -> None:
        try:
            time.sleep(delay_s)   # let the run reach its parked state first
            self.broker.claim(intervention_id, self.name)
            token = self.broker.grant_control(intervention_id)
            for step in steps:
                action = ActionKind(step["action"])
                self.broker.submit(lambda s=step, a=action: self.broker.operator_act(
                    intervention_id, token, kind=a,
                    handle=s.get("handle"), value=s.get("value"), key=s.get("key")),
                    timeout=60)
            self.broker.handback(intervention_id, token,
                                 resume=resume, resolution=resolution)
        except Exception as exc:      # surfaced to the test / CLI afterwards
            self.error = exc

    def find_handle(self, label: str, role: str | None = None) -> str | None:
        """Look up a control on the live session by label, from this thread."""
        obs = self.broker.submit(lambda: self.broker.surface.perceive(0), timeout=30)
        for node in obs.controls:
            if label.casefold() in (node.label or "").casefold():
                if role is None or node.role == role:
                    return node.handle
        return None
