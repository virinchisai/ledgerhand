# Evidence

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

## The discovery run

`discover_684b2d83/`

| | |
|---|---|
| model | `ollama/qwen2.5:7b-instruct` — local, free, no egress |
| tenant | `firstvalley`, policy profile `default` |
| outcome | **succeeded** — goal state reached; all declared outputs located |
| cost | 5 navigation decisions + 1 goal check, 1249s of inference, 1260.0s wall clock |

What the model actually did, read straight out of `run.jsonl`:

```
nav   step 1    363s  {"action":"type","target":"f0_e1","value":"@param:MCB_OPERATOR","reason":"Enter oper
        -> type 'Operator ID' ok=True
nav   step 2    111s  {"action":"type","target":"f0_e2","value":"@secret:MCB_PASSWORD","reason":"enter pas
        -> type 'Password' ok=True
nav   step 3    102s  {"action":"press","key":"Enter","reason":"submit sign-on"}
        -> press 'Enter' ok=True
nav   step 4    148s  {"action":"type","target":"f1_e1","value":"@param:member_id","reason":"enter member 
        -> type 'Member ID' ok=True
nav   step 5    146s  {"action":"click","target":"[f1_e2]","reason":"search for member"}
        -> click 'Search' ok=True
CHECK step 6    380s  {"savings_balance":"[f1_t18]", "member_name":"[f1_t6]"}
        == goal reached, outputs bound to {'savings_balance': 'f1_t18', 'member_name': 'f1_t6'}
```

Three things worth noticing:

* **The model never saw a value.** Every field was filled from a reference —
  `@secret:MCB_OPERATOR`, `@param:member_id` — resolved by the executor after
  the decision. The artifact carries the same references, which is why it is
  parameterised rather than hard-wired to the member it was recorded against.
* **It found its own route.** Nobody told it to submit the sign-on form with
  Enter rather than clicking the button. It chose that, and that is what got
  recorded and replays.
* **The two questions are asked separately.** `nav` picks the next action;
  `CHECK` asks only "are the wanted values on this screen, and where?". Fused
  into one question this model timed out at 900s on the detail screen and, on
  an earlier attempt, answered by clicking "New Search" — walking off the
  screen that held the answer. Split, it binds both outputs in one shot.
  See REPORT.md §1.

The first decision costs ~365s and the rest ~110–150s: that difference is the
model loading, not thinking, which is why `OllamaClient.warm()` exists.

---

## A discovery run that did not work

`discover_e9ca225a/`

Kept deliberately. This is the same loop attempting the eleven-step
account-opening flow, and it shows where a 7B model on CPU runs out of
road: 12 navigation decisions and 2 goal checks, 4468s of inference,
ending **None** — None.

The interesting part is the last few decisions. With the form completely
filled and the submit button on screen, the model clicked the account-type
dropdown, then re-selected it, then selected the other option — cycling
between two states rather than pressing the button. That is what prompted
the oscillation check in the stuck detector: flipping a dropdown changes
the screen every time, so a naive "did anything change" test reads it as
progress and the loop will happily spend its whole budget going nowhere.

`member.subaccount.open` was therefore recorded deterministically instead
(`scripts/record_reference_capability.py`), and its provenance says so.
The capability exists to exercise the replay-side guardrails; the genuine
LLM discovery run in this repo is the one above.

---

## The replays

Produced by `scripts/evidence.sh`, in order. Each is its own directory with
a structured `run.jsonl`, a `result.json` and screenshots.

| # | scenario | result | run |
|---|---|---|---|
| 1 | happy path — the member it was recorded against | ✅ **SUCCESS** | `replay_06bc472d` |
| 2 | a **different** member — proves it is parameterised, not a macro | ✅ **SUCCESS** | `replay_04310bc2` |
| 3 | member that does not exist | 🟡 **BUSINESS OUTCOME** `MEMBER_NOT_FOUND` | `replay_eb946a78` |
| 4 | restricted record — permission denial | 🟡 **BUSINESS OUTCOME** `ACCESS_DENIED` | `replay_d034d0bb` |
| 5 | caller passed a non-numeric member id | 🔴 **FAILED** | `replay_ff11c751` |
| 6 | unexpected maintenance interstitial injected | ✅ **SUCCESS** _(recovered MAINTENANCE_INTERSTITIAL)_ | `replay_f31a81c6` |
| 7 | 1.2s of injected latency | ✅ **SUCCESS** | `replay_fb0a9a0f` |
| 8 | HTTP 500 injected | 🔴 **FAILED** `APP_ERROR` | `replay_5bf459c6` |
| 9 | session expired mid-run | 🟣 **ESCALATED** `SESSION_EXPIRED` _(handed to an operator and returned)_ | `replay_6932dc9f` |
| 10 | **tenant B** — same capability, overlay applied | ✅ **SUCCESS** | `replay_0180e4e3` |
| 11 | invoked by name, as an AI agent would call it | ✅ **SUCCESS** | `invoke_0a545dda` |
| 12 | **draft** capability with an irreversible step, unattended — refused before touching the UI | 🔴 **FAILED** | `replay_98b0fd11` |
| 13 | same capability **attended** — a human is watching, so it proceeds | ✅ **SUCCESS** | `replay_d03f096b` |
| 14 | the **approved** capability, unattended — now permitted | ✅ **SUCCESS** | `replay_25418d1b` |
| 15 | deposit below the capability's own minimum — rejected by the input contract | 🔴 **FAILED** | `replay_910e7b72` |
| 16 | the host declines the posting — a business outcome on a committing capability | 🟡 **BUSINESS OUTCOME** `POSTING_UNAVAILABLE` | `replay_9d64e0d6` |

The three rows that carry the argument of the whole design:

* **3** is not a failure. `ok == true`, `outcome_code: MEMBER_NOT_FOUND`. The
  caller asked a question and got an answer.
* **9** is a failure, and says so with a step index, expected-vs-observed, a
  screenshot and a DOM snapshot — then hands the live session to a person and
  takes it back.
* **10** is the same artifact, unmodified, against a second institution whose
  build renames `Member ID` to `Member Number` and `/search` to `/lookup`. The
  difference is three overrides — one URL, two control labels — not a
  second recording.

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
