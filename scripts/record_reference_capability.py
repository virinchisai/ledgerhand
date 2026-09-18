#!/usr/bin/env python3
"""Record `member.subaccount.open` deterministically, without a model.

WHY THIS EXISTS, stated plainly because it matters for reading the evidence:

The LLM-discovered capability in this repo is `member.savings_balance.lookup`
-- six decisions, driven by a real model against the live UI, logged under
evidence/discover_*. That is the genuine discovery run.

`member.subaccount.open` is an eleven-step flow whose final click commits an
irreversible action, and it exists to exercise the *replay-side* guardrails:
risk classification, the approval gate, and validation outcomes. A local 7B on
CPU could not complete eleven steps reliably -- four attempts, each failing at
a different late step (see REPORT.md §7). Rather than present a discovery run
that did not happen, this script walks the same flow with a deterministic
driver and records the result through the *same* recorder.

So the artifact is real, the replay is real, and the guardrail demonstration is
real. What is not claimed is that a model found this route. The artifact's
provenance says so in `model` and `notes`, and this file is the whole method.
"""
from __future__ import annotations

import json
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx

from ledgerhand.agent.loop import DiscoveryAgent
from ledgerhand.agent.recorder import RecorderConfig, record
from ledgerhand.evidence.recorder import EvidenceWriter
from ledgerhand.goalspec import load_goal
from ledgerhand.products.profile import load_product
from ledgerhand.replay.engine import new_run_id
from ledgerhand.safety.policy import PolicyGate, load_profile
from ledgerhand.safety.redaction import Redactor
from ledgerhand.surface.web import WebSurface
from tests.oracle import OracleClient

ROOT = pathlib.Path(__file__).resolve().parents[1]
APP = "http://127.0.0.1:8848"

#: The route, expressed as "when this control is on screen, do this".
PLAN = [
    ("Operator ID", {"action": "type", "value": "@secret:MCB_OPERATOR"}),
    ("Password", {"action": "type", "value": "@secret:MCB_PASSWORD"}),
    ("Sign On", {"action": "click", "role": "button"}),
    ("Member ID", {"action": "type", "value": "@param:member_id"}),
    ("Search", {"action": "click", "role": "button"}),
    ("Open Sub-Account", {"action": "click", "role": "link"}),
    ("Account Type", {"action": "select", "value": "@param:account_type"}),
    ("Nickname", {"action": "type", "value": "@param:nickname"}),
    ("Initial Deposit", {"action": "type", "value": "@param:initial_deposit"}),
    ("Open Account", {"action": "click", "role": "button"}),
]
#: Bound by the label beside the value, not by the value itself: the account
#: number differs on every invocation, its label does not.
BIND = {"new_account_number": "label: New Account Number",
        "confirmation_number": "label: Confirmation No."}


def _load_env() -> None:
    """Take credentials from .env, falling back to the checked-in example.

    The mock app's logins are not secrets, but the code path that uses them is
    the real one: the recorder resolves @secret references from the
    environment, and without them every step fails in a way that looks like the
    walk got lost rather than like a missing variable.
    """
    import os
    for name in (".env", ".env.example"):
        path = ROOT / name
        if not path.exists():
            continue
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())
        return


def main() -> int:
    _load_env()
    try:
        httpx.post(f"{APP}/admin/reset", timeout=5)
    except Exception:
        print(f"the target app is not running at {APP}; start it with: ledgerhand serve-app")
        return 1

    goal, outcome_codes = load_goal(ROOT / "goals" / "member-subaccount-open.yaml")
    run_id = new_run_id("record")
    redactor = Redactor()
    evidence = EvidenceWriter(ROOT / "evidence", run_id, redactor)
    surface = WebSurface(headless=True)
    surface.start()
    try:
        result = DiscoveryAgent(
            surface, OracleClient(plan=list(PLAN), bind=dict(BIND)),
            PolicyGate(load_profile(ROOT / "policy.yaml")), evidence,
            redactor=redactor, attended=True,
        ).run(goal, max_steps=16)
        if not result.succeeded:
            print(f"walk did not reach the goal: {result.status} — {result.reason}")
            return 1

        artifact = record(result, load_product(ROOT / "products" / "meridian-core.yaml"),
                          config=RecorderConfig(outcome_codes=outcome_codes))
        artifact.provenance.notes = (
            "Recorded by walking the flow with a deterministic driver, not by an "
            "LLM discovery run: a local 7B could not complete this eleven-step "
            "flow (see REPORT.md section 7). The genuine LLM discovery run in "
            "this repo is member.savings_balance.lookup. This capability exists "
            "to exercise the replay-side guardrails -- risk classification, the "
            "approval gate, and validation outcomes."
        )
        path = ROOT / "artifacts" / f"{artifact.id}.v{artifact.version}.json"
        path.write_text(json.dumps(artifact.model_dump(mode="json"), indent=2))
        evidence.write_json("artifact.json", artifact)
        print(f"recorded {artifact.ref} -> {path.relative_to(ROOT)}")
        print(f"  steps {len(artifact.steps)}, max risk {artifact.max_step_risk.value}, "
              f"approval {artifact.approval.value}")
        print(f"  provenance.model = {artifact.provenance.model}")
        return 0
    finally:
        surface.close()


if __name__ == "__main__":
    raise SystemExit(main())
