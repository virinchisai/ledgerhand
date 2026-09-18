#!/usr/bin/env python3
"""Regenerate evidence/README.md from the runs actually on disk.

Written as a script rather than done by hand so the index cannot drift away
from the evidence it describes -- run it after scripts/evidence.sh.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
EVIDENCE = ROOT / "evidence"

#: Matches the order scripts/evidence.sh runs them in.
SCENARIOS = [
    "happy path — the member it was recorded against",
    "a **different** member — proves it is parameterised, not a macro",
    "member that does not exist",
    "restricted record — permission denial",
    "caller passed a non-numeric member id",
    "unexpected maintenance interstitial injected",
    "1.2s of injected latency",
    "HTTP 500 injected",
    "session expired mid-run",
    "**tenant B** — same capability, overlay applied",
    "invoked by name, as an AI agent would call it",
]

STATUS = {
    "success": ("SUCCESS", "✅"),
    "business_outcome": ("BUSINESS OUTCOME", "🟡"),
    "failed": ("FAILED", "🔴"),
    "escalated": ("ESCALATED", "🟣"),
}

HEADER = """# Evidence

Two halves, and the distinction matters:

* **`discover_*`** — a real LLM-driven run against the live UI, produced once by
  a local `qwen2.5:7b-instruct` via Ollama. This is the run that cannot be faked
  and is not reproducible on demand: it costs ~21 minutes of CPU inference.
* **`replay_*` / `invoke_*`** — the deterministic half. **No model is involved.**
  Reproduce all of it on any machine, with no model installed:

  ```bash
  ledgerhand serve-app                      # in one shell
  ./scripts/evidence.sh                     # in another
  python3 scripts/build_evidence_readme.py  # regenerates this file
  ```

Every file here was written through the redactor.

---
"""

FOOTER = """
---

## What to look at

If you read only three things:

1. **`artifacts/member.savings_balance.lookup.v1.json`** — what the model's run
   was compiled into. Steps carry `@secret:` / `@param:` references rather than
   values; each control has several ranked locator strategies; and the balance
   is anchored on `SAVINGS` rather than on an account number that changes per
   member.
2. **The not-found replay** — `status: business_outcome`, `outcome_code:
   MEMBER_NOT_FOUND`, `ok: true`. A correct answer, not a crash. Compare it with
   the session-expiry run, which is a hard failure carrying a screenshot, a DOM
   snapshot and expected-vs-observed.
3. **The escalation run** — `control_transfer` events walking
   `agent → none → operator → agent`, the operator's actions recorded inline,
   and control returning under a fresh token.
"""


def load(directory: pathlib.Path) -> dict | None:
    log = directory / "run.jsonl"
    if not directory.is_dir() or not log.exists():
        return None
    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    return {
        "dir": directory.name,
        "mtime": log.stat().st_mtime,
        "events": events,
        "start": next((e for e in events if e["kind"] in ("run_start", "replay_start")), {}),
        "end": next((e for e in events if e["kind"] in ("run_end", "replay_end")), {}),
    }


def discovery_section(run: dict) -> str:
    navs = [e for e in run["events"] if e["kind"] == "model_call"]
    checks = [e for e in run["events"] if e["kind"] == "goal_check"]
    inference = int(sum(e.get("latency_ms", 0) for e in navs + checks) / 1000)
    out = [
        "## The discovery run", "", f"`{run['dir']}/`", "",
        "| | |", "|---|---|",
        f"| model | `{run['start'].get('model')}` — local, free, no egress |",
        f"| tenant | `{run['start'].get('tenant')}`, policy profile "
        f"`{run['start'].get('policy_profile')}` |",
        f"| outcome | **{run['end'].get('status')}** — {run['end'].get('reason')} |",
        f"| cost | {len(navs)} navigation decisions + {len(checks)} goal check, "
        f"{inference}s of inference, {run['end'].get('duration_s')}s wall clock |",
        "", "What the model actually did, read straight out of `run.jsonl`:", "", "```",
    ]
    for e in run["events"]:
        kind = e["kind"]
        if kind == "model_call":
            out.append(f"nav   step {e['step']} {e['latency_ms'] / 1000:6.0f}s  {e['raw'][:84]}")
        elif kind == "goal_check":
            out.append(f"CHECK step {e['step']} {e['latency_ms'] / 1000:6.0f}s  {e['raw'][:84]}")
        elif kind == "action":
            out.append(f"        -> {e['action']} {e.get('control')!r} ok={e.get('ok')}")
        elif kind == "goal_reached":
            out.append(f"        == goal reached, outputs bound to {e.get('outputs')}")
    out += ["```", "", "Three things worth noticing:", "",
        "* **The model never saw a value.** Every field was filled from a reference —",
        "  `@secret:MCB_OPERATOR`, `@param:member_id` — resolved by the executor after",
        "  the decision. The artifact carries the same references, which is why it is",
        "  parameterised rather than hard-wired to the member it was recorded against.",
        "* **It found its own route.** Nobody told it to submit the sign-on form with",
        "  Enter rather than clicking the button. It chose that, and that is what got",
        "  recorded and replays.",
        "* **The two questions are asked separately.** `nav` picks the next action;",
        "  `CHECK` asks only \"are the wanted values on this screen, and where?\". Fused",
        "  into one question this model timed out at 900s on the detail screen and, on",
        "  an earlier attempt, answered by clicking \"New Search\" — walking off the",
        "  screen that held the answer. Split, it binds both outputs in one shot.",
        "  See REPORT.md §1.", "",
        "The first decision costs ~365s and the rest ~110–150s: that difference is the",
        "model loading, not thinking, which is why `OllamaClient.warm()` exists.",
    ]
    return "\n".join(out)


def replay_section(runs: list[dict]) -> str:
    rows = ["## The replays", "",
            "Produced by `scripts/evidence.sh`, in order. Each is its own directory with",
            "a structured `run.jsonl`, a `result.json` and screenshots.", "",
            "| # | scenario | result | run |", "|---|---|---|---|"]
    for i, run in enumerate(runs):
        name, mark = STATUS.get(run["end"].get("status", "?"), (run["end"].get("status", "?"), ""))
        note = f" `{run['end']['outcome']}`" if run["end"].get("outcome") else ""
        recovered = sorted({e["code"] for e in run["events"] if e["kind"] == "recovery"})
        if recovered:
            note += f" _(recovered {', '.join(recovered)})_"
        if any(e["kind"] == "handoff_complete" for e in run["events"]):
            note += " _(handed to an operator and returned)_"
        scenario = SCENARIOS[i] if i < len(SCENARIOS) else "—"
        rows.append(f"| {i + 1} | {scenario} | {mark} **{name}**{note} | `{run['dir']}` |")
    rows += ["", "The three rows that carry the argument of the whole design:", "",
        "* **3** is not a failure. `ok == true`, `outcome_code: MEMBER_NOT_FOUND`. The",
        "  caller asked a question and got an answer.",
        "* **9** is a failure, and says so with a step index, expected-vs-observed, a",
        "  screenshot and a DOM snapshot — then hands the live session to a person and",
        "  takes it back.",
        "* **10** is the same artifact, unmodified, against a second institution whose",
        "  build renames `Member ID` to `Member Number` and `/search` to `/lookup`. The",
        "  difference is a nine-line overlay, not a second recording."]
    return "\n".join(rows)


def main() -> int:
    runs = [r for r in (load(d) for d in EVIDENCE.iterdir()) if r]
    discovery = [r for r in runs if r["dir"].startswith("discover")]
    replays = sorted((r for r in runs if not r["dir"].startswith("discover")),
                     key=lambda r: r["mtime"])
    if not discovery:
        print("no discovery run found under evidence/", file=sys.stderr)
        return 1
    body = "\n".join([HEADER, discovery_section(discovery[0]), "\n---\n",
                      replay_section(replays), FOOTER])
    (EVIDENCE / "README.md").write_text(body)
    print(f"evidence/README.md: 1 discovery run, {len(replays)} replays")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
