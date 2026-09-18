# Ledgerhand

**An LLM works out how to do a task in a UI that has no API. The successful run
becomes a typed, reviewable capability. After that it replays deterministically,
with no model in the loop.**

Built for the case the brief describes: back-office banking applications with no
integration surface, where the only way in is to drive the screen the way an
operator would.

```
goal ──▶ LLM discovery loop ──▶ capability artifact ──▶ deterministic replay ──▶ outputs
             (once, live)          (typed, versioned)      (no model, every time)
                  │                                                │
                  └──────────────── stuck? ────────▶ human takes the live session ◀┘
```

---

## Architecture

Six layers. The boundaries are placed so the expensive things to change are
isolated from the cheap ones.

```
  goals/*.yaml
       │  goal + typed contract, declared by a person
       ▼
  ┌─────────────────┐   observe → decide → act        ┌──────────────┐
  │ DiscoveryAgent  │────────────────────────────────▶│              │
  │ (the only place │◀────────────────────────────────│   Surface    │
  │  a model runs)  │   UINode: role · name · value   │  (protocol)  │
  └────────┬────────┘          state · box · frame    └──────┬───────┘
           │ trace                                           │
           ▼                                          WebSurface  ⋯ DesktopSurface
  ┌─────────────────┐                                (Playwright)   (AX/UIA, not built)
  │    Recorder     │  prunes · parameterises · derives checkpoints
  └────────┬────────┘  rejects anything specific to this run
           │
           ▼
  artifacts/*.json ── typed inputs · typed outputs · declared outcomes
       │              ranked locators · checkpoints · risk · overlays
       │ + arguments
       ▼
  ┌─────────────────┐   same Surface protocol, no LLM client imported
  │  ReplayEngine   │──▶ SUCCESS · BUSINESS_OUTCOME · FAILED · ESCALATED
  └────────┬────────┘
           │ stuck / irreversible / hard failure
           ▼
  SessionBroker ── single control lease ──▶ operator console ──▶ resume
           
  PolicyGate + Redactor sit across both paths, enforced identically.
```

**How one request flows.** A goal file declares the contract — typed inputs,
typed outputs, which product outcomes apply. The agent perceives the screen as
`UINode`s (never selectors), picks one action from a closed vocabulary, and the
executor substitutes `@param:` / `@secret:` references so the model never sees a
value. On success the recorder compiles the trace into a capability. From then
on `ReplayEngine` executes that contract with no model: it resolves each control
through ranked locator strategies, evaluates the capability's **declared
outcomes before its checkpoints** — which is what makes "no such member" an
answer rather than a mystery — and returns a structured result.

**The seams that matter:**

| Seam | Why it is there |
|---|---|
| `Surface` protocol | The only layer that knows this is a browser. A desktop driver is a new producer of `UINode`s, not a redesign. |
| `CapabilityArtifact` | A contract, not a macro. Decouples what was discovered from how it was discovered. |
| `PolicyGate` | One enforcement path for discovery and replay — a guardrail only the exploratory path honours is not a guardrail. |
| `SessionBroker` | Makes "who is driving" a single-valued, enforced piece of state so a run can pause, cede and resume. |

Full reasoning and trade-offs: [`REPORT.md`](REPORT.md) §1.

---

## Quick start

Requires Python 3.10+ and [Ollama](https://ollama.com) for the discovery run.
Nothing else is needed — the target application ships with the repo and there
are no paid services anywhere in the stack.

```bash
git clone <this repo> && cd ledgerhand
python3 -m pip install -e ".[dev]"
python3 -m playwright install chromium

ollama pull qwen2.5:7b-instruct      # the discovery model; local, free
```

Prefer not to install anything into your environment? Every command below works
unchanged as `PYTHONPATH=src python3 -m ledgerhand.cli <command>`; `ledgerhand`
is just the console script for that. Project data (`policy.yaml`, `products/`,
`goals/`, `artifacts/`, `evidence/`) is resolved from the checkout, or from
`LEDGERHAND_HOME` if you set it.

Credentials for the **mock** application (there are no real ones anywhere):

```bash
cp .env.example .env && set -a && source .env && set +a
```

### The demo path

Five steps, in order. **Use two terminals** — the target application runs in the
foreground and must stay up:

```bash
# terminal 1 — leave this running
ledgerhand serve-app
```

```
Meridian Core (mock) on http://127.0.0.1:8848
  tenant A  http://127.0.0.1:8848/t/firstvalley/
  tenant B  http://127.0.0.1:8848/t/summitcu/
```

Everything below runs in terminal 2.

**1 — Discovery.** An LLM drives the live UI until the goal is met, then the run
is compiled into a capability. This is the only step that uses a model.

```bash
ledgerhand discover goals/member-savings-balance.yaml
```

> **This takes ~21 minutes** on a local 7B on CPU — six decisions at 100–400s
> each, plus a few minutes to load the weights. It is not hung. Add `--headful`
> to watch it drive the browser, or skip to step 2: a recorded artifact is
> already committed, so every later step works without running this.

It prints each recorded step as it compiles them, ending with:

```
status succeeded after 6 steps in 1260s — goal state reached; all declared outputs located
recorded member.savings_balance.lookup@v1 -> artifacts/member.savings_balance.lookup.v1.json
```

**2 — Inspect** what was recorded, the way a reviewer would read it:

```bash
ledgerhand show member.savings_balance.lookup
```

**3 — Replay** it deterministically with different arguments. No model runs here
— but the flow signs on, so the mock credentials must be in the environment
(step 0 above). A capability whose secret is unset fails with a clear message
rather than a stack trace:

```bash
ledgerhand replay member.savings_balance.lookup --arg member_id=23456
```

```
SUCCESS member.savings_balance.lookup@v1 -> ['savings_balance', 'member_name']
┏━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━┓
┃ output          ┃ value           ┃
┡━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━┩
│ savings_balance │ 27430.09        │
│ member_name     │ MARCUS OYELARAN │
└─────────────────┴─────────────────┘
evidence: evidence/replay_ca4e0cda
```

Note it returns `27430.09` as a **number**, not the string `"27,430.09"` the
screen shows — the output is typed, and the extraction declares the transform.
Takes about 10 seconds.

**4 — Replay into an exceptional state.** A member that does not exist is a
*business outcome*, not a crash — the caller gets a code, not a stack trace:

```bash
ledgerhand replay member.savings_balance.lookup --arg member_id=99999
```

```
BUSINESS_OUTCOME MEMBER_NOT_FOUND: No member exists for the supplied member id.
```

Exit code 0, `ok == true`. The caller asked a question and got an answer.

**5 — Watch the guardrail refuse.** A second capability, `member.subaccount.open`,
ends by clicking "Open Account" — policy classifies that as irreversible, so it
will not replay unattended until a human approves it:

```bash
# refused before touching the UI: irreversible step, capability is draft
ledgerhand replay member.subaccount.open --arg member_id=23456 \
    --arg account_type=SAVINGS --arg initial_deposit=150.00
# FAILED at step -1 (preflight): expected approved capability for unattended
# replay; observed ... contains an irreversible step and is draft

ledgerhand approve member.subaccount.open --state approved

# now permitted, because approval is the recorded human decision
ledgerhand replay member.subaccount.open --arg member_id=23456 \
    --arg account_type=SAVINGS --arg initial_deposit=150.00
```

Run the whole evidence pack — twenty scenarios — with `./scripts/evidence.sh`.

### Running without Ollama

Everything except step 1 is model-free, so the whole system can be exercised
with no model installed at all:

```bash
python3 -m pytest              # 90 tests, incl. full discover→record→replay
ledgerhand replay member.savings_balance.lookup --arg member_id=12345
```

The test suite substitutes a deterministic oracle for the model (`tests/oracle.py`).

**Which runs used a model.** `member.savings_balance.lookup` was discovered by a
real LLM driving the live UI — six decisions, logged under `evidence/discover_*`.
`member.subaccount.open` was **not**: a local 7B could not complete that
eleven-step flow (four attempts; the failed run is kept in `/evidence` and the
reasons are in REPORT.md §7), so it was recorded by walking the flow
deterministically via `scripts/record_reference_capability.py`. Its
`provenance.model` says `oracle`. It exists to exercise the replay-side
guardrails, and nothing claims a model found that route.

---

## What each piece does

| Path | What it is |
|---|---|
| `apps/legacy_bank/` | The target: a deliberately legacy servicing console. Table layouts, `ctl00$MainContent$…` control names, content in a nested frame, no test IDs, two tenants on one vendor product, and injectable runtime faults. |
| `src/ledgerhand/surface/` | The surface seam. `Surface` is the protocol; `WebSurface` is the Chromium driver. Perception returns roles, names and geometry — never selectors. |
| `src/ledgerhand/models/` | The typed vocabulary: the capability artifact, locators, conditions, results, interventions. |
| `src/ledgerhand/agent/` | The discovery loop, the prompt contract, and the recorder that compiles a trace into a capability. |
| `src/ledgerhand/replay/` | The deterministic executor, the locator resolver, the condition evaluator. |
| `src/ledgerhand/safety/` | The allowlist/risk gate and the redactor. |
| `src/ledgerhand/escalation/` | The session broker (control lease) and the operator console. |
| `products/meridian-core.yaml` | Vendor-product knowledge: the error taxonomy, shared by every capability and tenant. |
| `policy.yaml` | Guardrail profiles. |
| `goals/` | Discovery requests — where a capability's contract is declared before the model runs. |
| `overlays/` | Per-tenant specialisations of a base capability. `summitcu.yaml` is three overrides that let the same artifact run against a second institution. |
| `artifacts/` | The recorded capabilities themselves, as reviewable JSON. |
| `scripts/` | `evidence.sh` reproduces the whole evidence pack; `build_evidence_readme.py` regenerates the evidence index from the runs on disk; `record_reference_capability.py` records the committing capability deterministically (and documents why). |

---

## More commands

```bash
# Publish saved capabilities as a catalog an AI agent can call by name
ledgerhand catalog
ledgerhand catalog --as-tools            # function-calling schemas

# Invoke one the way an agent would, and print the JSON it gets back
ledgerhand invoke member.savings_balance.lookup --arg member_id=12345

# Unattended replay of an irreversible capability is gated on approval
ledgerhand approve member.subaccount.open --state approved

# Apply a tenant overlay, then run the same capability against that institution
ledgerhand overlay overlays/summitcu.yaml
ledgerhand replay member.savings_balance.lookup --tenant summitcu --arg member_id=12345

# Watch it drive the browser
ledgerhand discover goals/member-savings-balance.yaml --headful

# Escalation: park the run, hand the live session to a human, resume
ledgerhand replay member.savings_balance.lookup --arg member_id=12345 \
    --escalate --console-port 8787
```

### Injecting runtime conditions

The target app exposes a fault injector so exceptional states can be produced on
demand rather than waited for. It is **outside the agent's allowlist** by design
— an agent that can inject faults into the system it is driving can manufacture
the evidence that it succeeded.

```bash
curl -X POST localhost:8848/admin/chaos -d '{"interstitial": true}'      # unexpected dialog
curl -X POST localhost:8848/admin/chaos -d '{"expire_session": true}'    # session timeout
curl -X POST localhost:8848/admin/chaos -d '{"server_error": true}'      # HTTP 500
curl -X POST localhost:8848/admin/chaos -d '{"slow_ms": 1500}'           # transient slowness
curl -X POST localhost:8848/admin/chaos -d '{"force_validation": true}'  # host declines a post
```

Seeded members: `12345` (three accounts), `23456` (two accounts), `55555`
(restricted — permission denial), anything else → not found.

### If something does not work

| Symptom | Cause and fix |
|---|---|
| `ledgerhand: command not found` | The package is not installed. Either `pip install -e .`, or run everything as `PYTHONPATH=src python3 -m ledgerhand.cli <command>` — identical behaviour. |
| `discover` prints "warming the model…" and appears to hang | Expected. A cold 7B takes several minutes to load, then 100–400s per decision. The run log updates live: `tail -f evidence/discover_*/run.jsonl`. |
| `secret 'MCB_OPERATOR' is not present in the environment` | The mock credentials are not exported. `set -a && source .env.example && set +a`. It fails this way on purpose rather than with a stack trace. |
| `the target app is not running` | Start `ledgerhand serve-app` in another terminal and leave it up. |
| Playwright errors about a missing browser | `python3 -m playwright install chromium`. |
| `ConnectError` on port 11434 | Ollama is not running — `ollama serve`. Only step 1 needs it. |
| `origin ... not in allowlist` | The allowlist in `policy.yaml` is default-deny. Working behaviour, not a bug. |

---

## Configuration

| Variable | Default | Meaning |
|---|---|---|
| `LEDGERHAND_LLM` | `ollama` | Provider: `ollama` or `openai` (any OpenAI-shaped endpoint). |
| `LEDGERHAND_MODEL` | `qwen2.5:7b-instruct` | Model id. |
| `OLLAMA_HOST` | `http://127.0.0.1:11434` | Ollama endpoint. |
| `LEDGERHAND_LLM_KEY` | — | API key, if using a hosted provider. |
| `LEDGERHAND_MCB_OPERATOR` / `LEDGERHAND_MCB_PASSWORD` | — | Mock app credentials. Referenced by name from artifacts; the values never enter one. |

No secret is ever written into an artifact, a log, or an evidence file. Steps
carry `@secret:NAME` and `@param:NAME` references that are resolved at
invocation time — the discovery model is never shown a real value either.

---

## Evidence

`evidence/` holds a real LLM-driven discovery run and the replays taken from it.
See [`evidence/README.md`](evidence/README.md) for what each run shows.

## Design write-up

[`REPORT.md`](REPORT.md) — architecture, the artifact schema, determinism and the
error taxonomy, heterogeneity and multi-tenancy, escalation, safety, and what was
deliberately cut.
