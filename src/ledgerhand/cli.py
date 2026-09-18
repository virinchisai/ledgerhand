"""Command line entry points.

Thin on purpose: every command wires existing pieces together and prints a
result. The interesting behaviour lives in the modules, where it can be tested.
"""
from __future__ import annotations

import json
import os
import pathlib
import sys
import time
from typing import Any

import typer
from rich.console import Console
from rich.table import Table

from .agent.llm import build_client
from .agent.loop import DiscoveryAgent, build_intervention
from .agent.recorder import RecorderConfig, record
from .escalation.broker import LeasedSurface, SessionBroker
from .escalation.console import AutoOperator, serve_console
from .evidence.recorder import EvidenceWriter
from .goalspec import load_goal
from .models.artifact import CapabilityArtifact
from .models.enums import ApprovalState, ControlOwner, ReplayStatus
from .products.profile import load_product
from .replay.engine import ReplayEngine, new_run_id
from .safety.policy import PolicyGate, load_profile
from .safety.redaction import Redactor
from .surface.web import WebSurface

app = typer.Typer(add_completion=False, help="Record and replay computer-use capabilities.")
out = Console()

def _project_root() -> pathlib.Path:
    """Where policy.yaml, products/, goals/, artifacts/ and evidence/ live.

    These are project data, not package data: they are meant to be edited and
    reviewed in the repo. So the root is found rather than assumed --
    LEDGERHAND_HOME wins, then the checkout this module was imported from, then
    the working directory. Without this, a non-editable install silently
    resolves them inside site-packages.
    """
    override = os.environ.get("LEDGERHAND_HOME")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    here = pathlib.Path(__file__).resolve().parents[2]
    if (here / "policy.yaml").exists():
        return here
    return pathlib.Path.cwd()


ROOT = _project_root()
DEFAULT_POLICY = ROOT / "policy.yaml"
DEFAULT_PRODUCT = ROOT / "products" / "meridian-core.yaml"
ARTIFACTS = ROOT / "artifacts"
EVIDENCE = ROOT / "evidence"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def _load_artifact(path: pathlib.Path) -> CapabilityArtifact:
    return CapabilityArtifact.model_validate_json(path.read_text())


def _find_artifact(reference: str) -> pathlib.Path:
    """Accept a path, or a capability id, or `id@vN`."""
    candidate = pathlib.Path(reference)
    if candidate.exists():
        return candidate
    wanted_id, _, wanted_version = reference.partition("@")
    best: tuple[int, pathlib.Path] | None = None
    for path in sorted(ARTIFACTS.glob("*.json")):
        try:
            artifact = _load_artifact(path)
        except Exception:
            continue
        if artifact.id != wanted_id:
            continue
        if wanted_version and f"v{artifact.version}" != wanted_version:
            continue
        if best is None or artifact.version > best[0]:
            best = (artifact.version, path)
    if best is None:
        raise typer.BadParameter(f"no artifact matching {reference!r} in {ARTIFACTS}")
    return best[1]


def _parse_args(pairs: list[str]) -> dict[str, Any]:
    args: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise typer.BadParameter(f"--arg expects name=value, got {pair!r}")
        name, _, value = pair.partition("=")
        args[name.strip()] = value
    return args


def _print_result(result: Any) -> None:
    status = result.status
    colour = {"success": "green", "business_outcome": "yellow",
              "escalated": "magenta", "failed": "red"}.get(status.value, "white")
    out.print(f"\n[{colour}]{result.summary()}[/{colour}]")
    if result.outputs:
        table = Table(show_header=True, header_style="bold")
        table.add_column("output"); table.add_column("value")
        for key, value in result.outputs.items():
            table.add_row(key, str(value))
        out.print(table)
    if result.drift_notes:
        out.print("[yellow]drift:[/yellow] " + "; ".join(result.drift_notes))
    if result.failure:
        out.print(f"[red]expected[/red] {result.failure.expected}")
        out.print(f"[red]observed[/red] {result.failure.observed}")
        for attempt in result.failure.locator_attempts:
            out.print(f"  · {attempt}")
        if result.failure.screenshot:
            out.print(f"[dim]screenshot: {result.failure.screenshot}[/dim]")
    out.print(f"[dim]evidence: {result.evidence_dir}[/dim]")


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

@app.command("serve-app")
def serve_app() -> None:
    """Run the mock legacy banking console (the automation target)."""
    sys.path.insert(0, str(ROOT))
    from apps.legacy_bank.server import main
    out.print("[green]Meridian Core (mock)[/green] on http://127.0.0.1:8848")
    out.print("  tenant A  http://127.0.0.1:8848/t/firstvalley/")
    out.print("  tenant B  http://127.0.0.1:8848/t/summitcu/")
    main()


@app.command()
def discover(
    goal_file: str = typer.Argument(..., help="YAML discovery request"),
    model: str = typer.Option(None, "--model", help="e.g. ollama:qwen2.5:7b-instruct"),
    max_steps: int = typer.Option(16, "--max-steps"),
    headful: bool = typer.Option(False, "--headful", help="watch the browser"),
    policy: str = typer.Option(str(DEFAULT_POLICY), "--policy"),
    profile: str = typer.Option("default", "--profile"),
    product: str = typer.Option(str(DEFAULT_PRODUCT), "--product"),
    console_port: int = typer.Option(0, "--console-port",
                                     help="serve the operator console if the run gets stuck"),
) -> None:
    """Drive a live UI with an LLM until the goal is met, then record it."""
    goal, outcome_codes = load_goal(goal_file)
    gate = PolicyGate(load_profile(policy, profile))
    product_profile = load_product(product)
    run_id = new_run_id("discover")
    redactor = Redactor()
    evidence = EvidenceWriter(EVIDENCE, run_id, redactor)
    surface = WebSurface(headless=not headful)
    surface.start()
    broker = SessionBroker(surface, evidence)
    leased = LeasedSurface(surface, broker, ControlOwner.AGENT)

    out.print(f"[bold]discover[/bold] {goal.capability_id} on {goal.tenant}")
    out.print(f"  model   {model or os.environ.get('LEDGERHAND_LLM', 'ollama')}")
    out.print(f"  policy  {gate.profile.name}   evidence {evidence.dir}")

    try:
        client = build_client(model)
        if hasattr(client, "warm"):
            out.print("  warming the model…")
            client.warm()
        agent = DiscoveryAgent(leased, client, gate, evidence,
                               redactor=redactor, attended=True)
        started = time.monotonic()
        result = agent.run(goal, max_steps=max_steps)
        out.print(f"\nstatus [bold]{result.status}[/bold] after {len(result.trace)} steps "
                  f"in {time.monotonic() - started:.0f}s — {result.reason}")

        if not result.succeeded:
            request = broker.raise_intervention(build_intervention(result, broker.session_id))
            out.print(f"[magenta]escalated[/magenta] intervention {request.id}")
            if console_port:
                serve_console(broker, port=console_port)
                out.print(f"operator console: http://127.0.0.1:{console_port}/i/{request.id}")
                out.print("waiting for a human to take control and hand it back…")
                broker.wait_for_handback(request.id)
            raise typer.Exit(code=2)

        artifact = record(result, product_profile,
                          config=RecorderConfig(outcome_codes=outcome_codes))
        ARTIFACTS.mkdir(exist_ok=True)
        path = ARTIFACTS / f"{artifact.id}.v{artifact.version}.json"
        path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2))
        evidence.write_json("artifact.json", artifact)
        out.print(f"\n[green]recorded[/green] {artifact.ref} -> {path}")
        table = Table("step", "action", "target", "risk", "checkpoint")
        for step in artifact.steps:
            table.add_row(str(step.index), step.action.value,
                          (step.target.description if step.target else step.url or step.key or ""),
                          step.risk.value,
                          step.checkpoint.describe() if step.checkpoint else "-")
        out.print(table)
    finally:
        surface.close()


@app.command()
def replay(
    artifact_ref: str = typer.Argument(..., help="artifact path, capability id, or id@vN"),
    arg: list[str] = typer.Option([], "--arg", help="name=value (repeatable)"),
    tenant: str = typer.Option(None, "--tenant", help="apply a tenant overlay"),
    attended: bool = typer.Option(False, "--attended",
                                  help="a human is watching; allows irreversible steps"),
    headful: bool = typer.Option(False, "--headful"),
    policy: str = typer.Option(str(DEFAULT_POLICY), "--policy"),
    profile: str = typer.Option("default", "--profile"),
    escalate: bool = typer.Option(False, "--escalate",
                                  help="raise an intervention instead of returning FAILED"),
    console_port: int = typer.Option(0, "--console-port"),
    auto_operator: bool = typer.Option(False, "--auto-operator",
                                       help="drive the handoff programmatically (for evidence)"),
) -> None:
    """Replay a capability deterministically. No model is consulted."""
    path = _find_artifact(artifact_ref)
    artifact = _load_artifact(path)
    gate = PolicyGate(load_profile(policy, profile))
    run_id = new_run_id("replay")
    redactor = Redactor()
    evidence = EvidenceWriter(EVIDENCE, run_id, redactor)
    surface = WebSurface(headless=not headful)
    surface.start()
    broker = SessionBroker(surface, evidence)
    leased = LeasedSurface(surface, broker, ControlOwner.AGENT)

    out.print(f"[bold]replay[/bold] {artifact.ref} ({path.name}) "
              f"tenant={tenant or artifact.target.tenant} attended={attended}")
    try:
        use_broker = escalate or console_port or auto_operator
        engine = ReplayEngine(leased, gate, evidence, redactor=redactor, attended=attended,
                              broker=broker if use_broker else None,
                              session_id=broker.session_id,
                              handoff_timeout_s=60.0 if auto_operator else 900.0)
        if console_port:
            serve_console(broker, port=console_port)
            out.print(f"operator console: http://127.0.0.1:{console_port}/")
        if auto_operator:
            _arm_auto_operator(broker)
        result = engine.run(artifact, _parse_args(arg), tenant=tenant)
        _print_result(result)

        if result.intervention_id:
            request = broker.interventions.get(result.intervention_id)
            out.print(f"[magenta]intervention[/magenta] {result.intervention_id} "
                      f"({request.status.value if request else '?'}) — "
                      f"control held by {broker.lease.owner.value}")
            if request and request.operator_actions:
                for action in request.operator_actions:
                    out.print(f"  · {action.kind} {action.target} {action.note[:50]}")
        _update_stability(path, artifact, result)
        raise typer.Exit(code=0 if result.ok else 1)
    finally:
        surface.close()


def _arm_auto_operator(broker: SessionBroker) -> None:
    """Watch for an intervention and drive the handoff without a human.

    Used to produce reproducible escalation evidence. It goes through the same
    claim / take-control / act / hand-back API the HTML console posts to.
    """
    import threading

    def watch() -> None:
        operator = AutoOperator(broker)
        for _ in range(600):
            pending = broker.pending
            if pending:
                request = pending[0]
                out.print(f"[magenta]scripted operator taking control[/magenta] "
                          f"of {request.id}")
                operator.run_async(
                    request.id, steps=[],
                    resolution="operator inspected the stopped session and released it",
                    resume=True)
                return
            time.sleep(0.25)

    threading.Thread(target=watch, name="auto-operator-watch", daemon=True).start()


def _update_stability(path: pathlib.Path, artifact: CapabilityArtifact, result: Any) -> None:
    """Fold this run into the artifact's replay history."""
    from datetime import datetime, timezone
    stability = artifact.stability
    stability.replays += 1
    if result.status is ReplayStatus.SUCCESS:
        stability.successes += 1
    elif result.status is ReplayStatus.BUSINESS_OUTCOME:
        stability.business_outcomes += 1
    else:
        stability.failures += 1
    stability.last_replay_at = datetime.now(timezone.utc)
    for report in result.steps:
        if report.locator and report.locator.used_fallback:
            key = report.locator.resolved_by.value
            stability.fallback_counts[key] = stability.fallback_counts.get(key, 0) + 1
    path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2))


@app.command()
def catalog(as_tools: bool = typer.Option(False, "--as-tools",
                                          help="emit function-calling schemas")) -> None:
    """List saved capabilities as a catalog an AI agent can discover."""
    artifacts = []
    for path in sorted(ARTIFACTS.glob("*.json")):
        try:
            artifacts.append(_load_artifact(path))
        except Exception as exc:
            out.print(f"[red]skipping {path.name}: {exc}[/red]")
    if as_tools:
        out.print_json(json.dumps([a.tool_schema() for a in artifacts], indent=2))
        return
    table = Table("capability", "ver", "tenant", "approval", "inputs", "outputs",
                  "outcomes", "replays", "risk")
    for a in artifacts:
        rate = a.stability.success_rate
        replays = f"{a.stability.replays}" + (f" ({rate:.0%})" if rate is not None else "")
        table.add_row(a.id, str(a.version), a.target.tenant, a.approval.value,
                      ",".join(p.name for p in a.inputs),
                      ",".join(o.name for o in a.outputs),
                      str(len(a.outcomes)), replays, a.max_step_risk.value)
    out.print(table)


@app.command()
def approve(
    artifact_ref: str = typer.Argument(...),
    state: str = typer.Option("approved", "--state", help="draft|approved|revoked"),
) -> None:
    """Move a capability through the approval gate that unattended replay checks."""
    path = _find_artifact(artifact_ref)
    artifact = _load_artifact(path)
    artifact.approval = ApprovalState(state)
    path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2))
    out.print(f"{artifact.ref} -> [bold]{state}[/bold]")


@app.command()
def show(artifact_ref: str = typer.Argument(...)) -> None:
    """Print a capability in the form a human reviewer reads it."""
    artifact = _load_artifact(_find_artifact(artifact_ref))
    out.print(f"[bold]{artifact.ref}[/bold] — {artifact.name}")
    out.print(f"{artifact.description}\n")
    out.print(f"target   {artifact.target.product} {artifact.target.product_version} "
              f"tenant={artifact.target.tenant} surface={artifact.target.surface.value}")
    out.print(f"recorded {artifact.provenance.recorded_at:%Y-%m-%d %H:%M} by "
              f"{artifact.provenance.model} (run {artifact.provenance.discovery_run_id})")
    out.print(f"approval {artifact.approval.value}   max risk {artifact.max_step_risk.value}   "
              f"fingerprint {artifact.fingerprint()}\n")

    inputs = Table("input", "type", "req", "sensitivity", "constraint")
    for p in artifact.inputs:
        inputs.add_row(p.name, p.type.value, "yes" if p.required else "no",
                       p.sensitivity.value, p.pattern or (",".join(p.enum or []) or "-"))
    out.print(inputs)

    outputs = Table("output", "type", "sensitivity", "read from")
    for o in artifact.outputs:
        primary = o.extract.control.primary
        outputs.add_row(o.name, o.type.value, o.sensitivity.value,
                        primary.describe() if primary else "-")
    out.print(outputs)

    steps = Table("step", "action", "target", "locator (primary)", "risk", "checkpoint")
    for s in artifact.steps:
        primary = s.target.primary if s.target else None
        steps.add_row(str(s.index), s.action.value + (" (optional)" if s.optional else ""),
                      (s.target.description if s.target else s.url or s.key or ""),
                      primary.describe() if primary else "-", s.risk.value,
                      s.checkpoint.describe() if s.checkpoint else "-")
    out.print(steps)

    outcomes = Table("outcome", "class", "detector")
    for o in artifact.outcomes:
        outcomes.add_row(o.code, o.classification.value, o.detect.describe()[:70])
    out.print(outcomes)
    if artifact.success_condition:
        out.print(f"\nsuccess when: {artifact.success_condition.describe()}")
    if artifact.overlays:
        out.print("\noverlays: " + ", ".join(o.tenant for o in artifact.overlays))


@app.command()
def overlay(
    overlay_file: str = typer.Argument(..., help="YAML tenant overlay"),
    artifact_ref: str = typer.Option(None, "--artifact",
                                     help="defaults to the capability named in the file"),
) -> None:
    """Attach a tenant overlay to a capability.

    An overlay is a small reviewable diff against one base capability, not a
    second recording. Applying it here keeps the base as the single source of
    truth for the flow's shape.
    """
    import yaml as _yaml
    from .models.artifact import TenantOverlay
    from .products.profile import parse_condition, parse_control

    data = _yaml.safe_load(pathlib.Path(overlay_file).read_text()) or {}
    path = _find_artifact(artifact_ref or data["capability"])
    artifact = _load_artifact(path)

    def resolve_key(key: str) -> str:
        """Allow overlays to be authored against labels, not step numbers.

        `step:Member ID` is stable across re-recordings; `steps[3].target` is
        not. The symbolic form is resolved here and the canonical, indexed form
        is what gets written into the artifact -- so the authored file stays
        readable and the stored overlay stays precise.
        """
        if not key.startswith("step:"):
            return key
        wanted = key[len("step:"):].strip().casefold()
        hits = [st.index for st in artifact.steps
                if st.target and wanted in st.target.description.casefold()]
        if len(hits) != 1:
            raise typer.BadParameter(
                f"{key!r} matches {len(hits)} steps in {artifact.ref}; "
                f"steps are: " + ", ".join(
                    f"{st.index}={st.target.description}" for st in artifact.steps if st.target))
        return f"steps[{hits[0]}].target"

    entry = TenantOverlay(
        tenant=data["tenant"],
        product_version=data.get("product_version"),
        url_overrides=dict(data.get("url_overrides") or {}),
        control_overrides={resolve_key(k): parse_control(v)
                           for k, v in (data.get("control_overrides") or {}).items()},
        outcome_overrides={k: parse_condition(v)
                           for k, v in (data.get("outcome_overrides") or {}).items()},
        notes=data.get("notes", ""),
    )
    artifact.overlays = [o for o in artifact.overlays if o.tenant != entry.tenant] + [entry]
    path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2))

    resolved = artifact.resolve_for(entry.tenant)
    out.print(f"{artifact.ref} + overlay [bold]{entry.tenant}[/bold] -> {path.name}")
    table = Table("what", "base", f"{entry.tenant}")
    table.add_row("entry_url", artifact.target.entry_url, resolved.target.entry_url)
    for key in entry.control_overrides:
        if key.startswith("steps["):
            idx = int(key[len("steps["):key.index("]")])
            table.add_row(f"step {idx}", artifact.steps[idx].target.description,
                          resolved.steps[idx].target.description)
    out.print(table)
    if entry.notes:
        out.print(f"[dim]{entry.notes}[/dim]")


@app.command()
def invoke(
    capability: str = typer.Argument(..., help="capability id, as an agent would call it"),
    arg: list[str] = typer.Option([], "--arg"),
    tenant: str = typer.Option(None, "--tenant"),
) -> None:
    """Invoke a capability by name and print the JSON an agent would receive."""
    path = _find_artifact(capability)
    artifact = _load_artifact(path)
    gate = PolicyGate(load_profile(str(DEFAULT_POLICY), artifact.target.policy_profile))
    redactor = Redactor()
    evidence = EvidenceWriter(EVIDENCE, new_run_id("invoke"), redactor)
    surface = WebSurface(headless=True)
    surface.start()
    try:
        engine = ReplayEngine(surface, gate, evidence, redactor=redactor, attended=False)
        result = engine.run(artifact, _parse_args(arg), tenant=tenant)
        payload = {
            "capability": result.capability,
            "status": result.status.value,
            "outputs": result.outputs,
            "outcome": result.outcome_code,
            "message": result.message,
            "error": result.failure.observed if result.failure else None,
        }
        print(json.dumps(payload, indent=2, default=str))
    finally:
        surface.close()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
