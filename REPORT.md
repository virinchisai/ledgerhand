# Ledgerhand — design write-up

The through-line, because every decision below follows from it:

> **The model discovers. The artifact is the capability. Deterministic replay is
> how the agent invokes it in production.**

A model runs exactly once per capability, during discovery. Everything after
that is a typed contract and an executor. The interesting engineering is not in
the agent loop — it is in what the loop is allowed to emit, and in what the
executor does when the screen is not what was recorded.

---

## 1. Architecture

Six layers, with the boundaries chosen so that the expensive things to change
are isolated from the cheap ones.

```
  goal.yaml ──▶ DiscoveryAgent ──▶ Recorder ──▶ CapabilityArtifact (JSON)
                     │                                  │
                     │  observe/decide/act              │  typed contract
                     ▼                                  ▼
              ┌─────────────┐                   ┌───────────────┐
              │   Surface   │◀──────────────────│ ReplayEngine  │
              │  (protocol) │   same protocol   │  (no model)   │
              └──────┬──────┘                   └───────┬───────┘
                     │                                  │
              WebSurface / …                    PolicyGate · Redactor
                     │                                  │
              SessionBroker ── control lease ── operator console
```

* **Surface** is the only thing that knows what kind of application this is. It
  answers two questions: *what is on screen* (as `UINode`s — role, accessible
  name, value, state, geometry, frame path) and *do one semantic action to one
  of those nodes*. It exposes no selectors, no DOM handles, no window IDs.
* **DiscoveryAgent** is the only place a model ever decides anything.
* **Recorder** compiles a trace into a contract. This is a compiler, not a
  serialiser: it prunes, parameterises, derives checkpoints and rejects
  run-specific data.
* **ReplayEngine** executes that contract. It imports no LLM client.
* **PolicyGate** and **Redactor** are used identically by both paths.
* **SessionBroker** owns the live session and the single lease over it.

### Decisions worth defending

**Perception is role-and-name-first, and computed in-page.** I started with
Chrome's own accessibility tree over CDP, and it is the right *shape* — but on
the markup this system targets it is not sufficient. A text box whose label is a
sibling `<td>` has an **empty accessible name**; I verified this before building
anything else. Recovering that label from layout is the actual job, and it has
to happen where the layout is. So `perception.js` computes an AX-shaped
projection: accessible name by the ARIA rules browsers agree on, then a ranked
set of legacy fallbacks (the cell to the left, preceding text, the generated
`ctl00$…$txtMemberId` name de-camelised). The *output shape* is what a macOS AX
or Windows UIA tree already gives you, which is what makes a desktop driver a
producer swap rather than a redesign.

**Acting is real input at real coordinates.** Clicks and keystrokes go through
the mouse and keyboard at the control's actual position, not `element.click()`.
That is what "computer use" means and it is the part that ports to a desktop
driver. One exception: a native `<select>` has no screen-level affordance a
mouse can drive reliably, so the driver uses the platform widget API. A desktop
driver makes the same trade with `AXSetValue`. The seam is drawn at *semantic
action on a perceived node*, precisely so each driver can make that call.

**The model is local (Ollama, `qwen2.5:7b-instruct`).** Two reasons, one of
which is a constraint I set deliberately. Discovery reads live back-office
screens; with a local model, member names and balances never leave the host,
which removes a whole class of data-handling question from the discovery path.
And a 7B model at ~1.4 tokens/sec is a *harder* test: if the loop completes a
real multi-step flow under that, the loop design is carrying the weight. A
frontier model would have made a weak loop look fine. Provider is a seam
(`agent/llm.py` ships an OpenAI-compatible client too); it is not load-bearing,
because the model runs once.

**The target is a purpose-built hostile application, not a public demo site.**
The two hardest requirements — an error taxonomy that separates business
outcomes from failures, and reuse across tenants running one vendor product —
need *injectable* runtime faults and *two* tenants of the same product. No
public site offers either, and automating one adds terms-of-service risk for
nothing. So `apps/legacy_bank` is built out of the failure modes named in the
brief: table layouts, `ctl00$MainContent$…` control names, content in a nested
frame, no test IDs, no `<label for>`, and a fault injector for not-found,
permission denial, validation rejection, unexpected interstitials, session
expiry, slow loads and HTTP 500. The cost of this choice is real: a mock target
cannot prove robustness against the full diversity of real markup. What it buys
is that every claim below is *tested*, not asserted.

What that cost in practice, from the run in `/evidence`: **6 decisions, 21
minutes** — five navigation steps at 100–150s each plus one goal check. The
replay taken from it runs in **9.8 seconds with no model at all**. That ratio is
the entire argument for the record-once/replay-many shape: discovery is a
one-time expense amortised over every future invocation, which is exactly why it
is affordable to run it on a slow local model and exactly why the production
path must not contain one.

**The model's decision surface is deliberately tiny, and sized per turn.** The
loop hands the model a numbered list of controls and a closed action vocabulary,
and asks for one JSON object. It never asks for a selector, a URL, or a value —
everything it could get subtly wrong has been moved out of its reach. Two
details earned their place by failing first on real runs:

* The action menu is **assembled from what is on screen**. On a read-only detail
  page the model is offered only `click` and `give_up`. When `select` was always
  on the menu, the 7B tried to "select SAVINGS" on a table cell — a move that is
  not merely wrong but *unrepresentable* once the menu is built from the screen.
* References carry their meaning. Parameters always had descriptions; secrets
  were rendered as bare names, and the model duly typed the member id into the
  operator-id field. `@secret:MCB_OPERATOR — the operator id to sign on with`
  fixed it. A reference is only useful if its meaning travels with it.

**And the loop asks two questions, not one.** This is the change that actually
made the run succeed, and it started as a bug. The loop originally asked "what
should you do next?" with `finish` on the menu — folding *what do I do* together
with *am I done, and where are the values*. Those are different questions: the
second is reading comprehension over one screen and needs no history, no
controls and no action vocabulary. Fused, the detail screen produced a
~900-token prompt that this model timed out on at 900s, and on an earlier
attempt answered by clicking "New Search" — walking off the screen that held the
answer. Split, the goal check is ~170 tokens and binds both outputs in one shot,
and the navigation prompt got ~20% smaller for losing the data it never needed.

The goal check is gated on the screen actually showing data (eight or more
readable nodes), because on CPU inference an extra call costs minutes and on a
sign-on form the answer is certainly "no".

All three are the same lesson: when a small model gets something wrong, the
first question is whether the loop gave it the chance to.

**One process, synchronous.** The unit of work is one live browser session,
which is stateful and long-lived; splitting it across services would mean
shipping a session between workers. Scaling this is "more session workers", and
the API-shaped seam that would need (`catalog` / `invoke` by name with typed
args) already exists. Building queues now would be infrastructure without a
load to justify it.

---

## 2. Artifact schema

The stance in one line: **a capability is an API contract that happens to be
implemented by driving a UI.** So it is shaped like an API — typed inputs, typed
outputs, a declared set of outcomes, a version — and not like a macro recording.
The step list is an implementation detail of the contract.

```
CapabilityArtifact
  schema_version, id, version, name, description
  target      TargetBinding   product · product_version · tenant · surface · entry_url
  inputs      [ParamSpec]     type · required · sensitivity · pattern/enum/range
  outputs     [OutputSpec]    type · sensitivity · ExtractionSpec(control, attribute, transform)
  outcomes    [OutcomeSpec]   code · classification · detector · recovery
  steps       [Step]          action · ControlDescriptor · ValueRef · wait · checkpoint · risk · optional
  success_condition  Condition
  policy      CapabilityPolicy      provenance  Provenance
  approval    draft|approved|revoked  stability  StabilityRecord
  overlays    [TenantOverlay]
```

Five choices carry the design.

**Values are references, never literals.** A `ValueRef` is
`{source: literal|param|secret|output, ref: name}`. `@secret:MCB_PASSWORD` and
`@param:member_id` carry a *name*; the value is resolved at invocation. This is
not only about what lands in the file. The discovery model emits those
references too — it is told a parameter exists and what it means, and is never
shown a value. So the model completes a real member lookup **without ever seeing
a member number**, and parameterisation in the artifact is *exact* rather than
inferred by matching recorded strings back out of a trace. A test asserts that
the discovery run's member id, the operator id and the password appear nowhere
in the serialised artifact.

**Targeting is a ranked bundle of hypotheses, not a selector.** A
`ControlDescriptor` holds several `LocatorStrategy` entries, ordered
`ax_role_name → label_text → anchor_relative → dom_attr → dom_css → ordinal`.
That order encodes the core bet: on legacy enterprise UIs, *what a control is
and what it is called* outlives *where it sits in the markup*. Crucially the
ranking is not hardcoded per kind — at record time each candidate is **tested
against the observation it was recorded in** and kept only if it uniquely
identifies the node, with confidence assigned from that test. A strategy already
ambiguous at record time would certainly be ambiguous at replay, so it is
demoted rather than written out as though it were sound. When nothing identifies
a control but its position, an `ordinal` strategy is recorded honestly at
confidence 0.2, so review can catch it.

**Outcomes are declared, and they are product-level.** This is the honest answer
to "how did you discover the error paths?" — you cannot, from a successful run.
So `products/meridian-core.yaml` declares them once: `MCB-0042` means no such
member, on every screen, in every institution running Meridian Core 7.x, for
every capability anyone records. A newly recorded capability inherits correct
handling for paths its discovery run never visited; fixing a detector fixes it
everywhere; a tenant whose wording differs overrides one detector rather than
re-recording a flow. A capability narrows the set it claims — a read-only lookup
does not advertise that it can return a posting rejection.

**Provenance deliberately excludes the transcript.** The artifact records which
run produced it, which model, how many raw steps were pruned, and a surface
fingerprint. The transcript itself lives in `evidence/` under that run id. Two
reasons: the artifact stays reviewable, and model chatter quotes screen contents
— which on these screens is regulated data — so keeping it out of a document
that gets committed is a data-handling decision, not tidiness.

**Tenant difference is an overlay, not a second artifact.** See §4.

The artifact also publishes itself as a function-calling schema
(`tool_schema()`), so `ledgerhand catalog --as-tools` emits a catalog an agent
can discover and invoke by name with typed args. That fell out of typing the
contract properly rather than being built separately.

---

## 3. Determinism & error handling

### What makes replay deterministic

* **No model.** `replay/` imports no LLM client.
* **The resolver and the condition evaluator are pure functions** over an
  `Observation`. No browser, no I/O. Every locator, ambiguity and drift case is
  unit-tested without launching anything — and the same code serves a desktop
  driver unchanged.
* **Waits are conditions, not clocks.** Each step records `wait.until` — usually
  the same condition as its checkpoint. `before_ms` exists for surfaces with no
  observable settle signal, and using it is recorded so it shows up in review.
* **Resolution requires a unique match.** Two matches is an ambiguity, reported
  and stepped past, never guessed between.
* **Arguments are validated before the surface is touched.** Type, pattern,
  enum, range. Passing a letter where a member id goes must not arrive at the
  caller disguised as a validation error from the bank.
* **A step without a checkpoint cannot tell you it failed**, so the recorder
  attaches one wherever the screen actually changed. In the recorded artifact
  the typing steps carry none and the submitting steps do — which is correct
  rather than a gap: typing into a field changes no observable state, so any
  assertion after it would be either vacuous or a lie. The assertions land on
  the steps that move between screens, and the last one carries the success
  condition, which additionally requires every output control to resolve.

Two recorder bugs are worth naming because they are the ones that produce
artifacts which pass their own replay and fail on the second invocation, and
both were caught by tests that replay with *different* arguments:

* **Anchoring on an identifier.** For the row
  `0001-4471 | SAVINGS | ACTIVE | 4,182.55`, the most distinctive cell is the
  account number — and it is per-record, so anchoring there yields a capability
  that works for exactly one member. The recorder now prefers *categorical*
  cells (no digits, not a generic status word): it anchors on `SAVINGS`, which
  comes from a vocabulary the vendor controls.
* **Checkpointing on record data.** Reaching the detail screen surfaces both
  `Member Detail` and `MAIN` as new text. `MAIN` is member 12345's branch.
  Checkpoint derivation now hard-rejects the run's own parameter values,
  anything containing a digit, anything rendered as a control's value, and
  generic chrome — then scores what is left on casing and position, because on
  these screens chrome is title-cased and appears above the data.

That scoring is a heuristic and it is the weakest link in the recorder. It is
backed rather than trusted: the success condition independently requires every
output control to resolve, and the checkpoint is written into the artifact in
plain text where a reviewer sees it before approval.

### The error taxonomy

Three classes, one detector language, declared per product:

| Class | Meaning | Result |
|---|---|---|
| `business_outcome` | A legitimate answer the caller asked for | `status=business_outcome`, `outcome_code`, `ok == True` |
| `recoverable` | Clearable; the run continues | recovery steps run, step retried, recorded |
| `hard_failure` | Stop and surface it | `status=failed` + `FailureDetail` + screenshot + DOM snapshot |

**The ordering is the most important line in the engine.** After every step,
declared outcomes are evaluated *before* the checkpoint. A not-found screen will
not satisfy the checkpoint either — so evaluating the checkpoint first would
report "expected `Member Detail`, observed something else" for what is actually
a correct answer. Evaluating outcomes first is what makes `MEMBER_NOT_FOUND`
return as an answer instead of surfacing as a mystery.

`ReplayResult.ok` is true for `success` **and** `business_outcome`, and distinct
from `status is SUCCESS`, which additionally means the outputs came back.
Callers branch on a code; they never parse a message.

Concretely, as tested: member `99999` → `MEMBER_NOT_FOUND`; member `55555` →
`ACCESS_DENIED`; an injected maintenance interstitial → cleared, run completes,
recovery recorded; injected session expiry → `SESSION_EXPIRED` hard failure with
the failing step, expected vs observed, screenshot and markup dump; HTTP 500 →
hard failure; 900 ms of injected latency → absorbed.

Session expiry is deliberately **not** recoverable. Re-authenticating mid-flow
would silently resume a run whose earlier steps may already have half-committed,
and needs credentials the replay path is not given. Surfacing it is correct.

### Drift

Secondary here, because these UIs change slowly — but not ignored. Replay
records *which* hypothesis resolved each control; resolving by anything but the
primary sets `drift_detected` and appends a note, and the counters accumulate
into the artifact's `StabilityRecord`. So the system tells you a vendor renamed
a field the first time it happens, while still completing the run. A surface
fingerprint over control labels is compared at the end for the same reason.

---

## 4. Heterogeneity & multi-tenant

### Extending to other surfaces

The seam is `Surface`: *perceive the current state as `UINode`s* and *perform
one semantic action on one perceived node*. `UINode` carries only what a macOS
AX tree or a Windows UIA tree also expose — role, name, value, state flags,
bounding box, frame/window path.

Everything above that line is already surface-independent: the artifact schema
names roles and accessible names, the resolver and condition evaluator are pure
functions over `UINode` lists, the outcome taxonomy is text and control
assertions, and the policy gate reasons about action kinds and labels.

A **desktop driver** therefore implements the protocol and nothing else changes:
`perceive()` walks the AX/UIA tree (which, unlike the web case, already computes
accessible names); `act()` uses `AXPress`/`AXSetValue` or synthesised input at
the element's screen rect; `frame_path` becomes a window/pane path;
`current_url()` becomes a window identity. The two strategies that do not carry
over are `dom_attr` and `dom_css` — which is the point of ranking them last. A
**legacy web app** is the case already implemented.

What would genuinely need work: surfaces with no addressable element model at
all (a terminal emulator, a Citrix pixel stream). There the honest answer is
OCR plus coordinates, `UINode.box` becomes the only handle, and `ordinal`-style
targeting stops being a last resort. The schema survives; confidence does not.

### Reuse across tenants

Hundreds of tenants, ~20 apps each, many running the same vendor product. Three
mechanisms, no per-tenant rebuilds:

1. **Product profile.** The error taxonomy, recovery flows and risk labels
   belong to `meridian-core`, not to a capability or a tenant. Written once.
2. **Base capability + `TenantOverlay`.** An overlay is a small reviewable diff
   — control overrides keyed by path (`steps[3].target`,
   `outputs[savings_balance].extract.control`), URL overrides, outcome-detector
   overrides. `resolve_for(tenant)` returns a specialised artifact, so the
   replay engine never knows overlays exist. The base stays the single source of
   truth for the flow's *shape*; a tenant that renamed `Member ID` to
   `Member Number` and moved `/search` to `/lookup` overrides two lines rather
   than owning a divergent copy. Tenant B in this repo is exactly that case.
3. **Normalisation as the first line of defence.** Locator matching folds case,
   whitespace, non-breaking spaces and trailing label punctuation by default,
   so a large share of cross-tenant difference never needs an overlay at all.

**Detecting per-tenant and per-version drift** uses the same signals as §3,
which is the payoff of recording *how* each control resolved: a tenant whose
capability starts resolving by `dom_attr` instead of `label_text` is telling you
its labels changed, per tenant, per run, without anyone writing a drift
detector. `TargetBinding.product_version` and the overlay's version field give
the axis to attribute it to.

Not built: automatic overlay generation from a diff of two tenants, route
canonicalisation (`/item/12345` → `/item/:id`), and enforcement of version
ranges. The data to do the first two is present; see §7.

---

## 5. Escalation & handoff

### Detecting stuck

Six triggers, because "stuck" has genuinely different causes:

* **No progress** — a digest of URL + control labels is unchanged across four
  consecutive steps. This is the honest definition, and it is what catches a
  loop that is happily acting and going nowhere. Deliberately coarse: a page
  whose only change is a spinner is not progress.
* **Unusable model output** twice in a row (schema-invalid, or naming a handle
  that is not on screen).
* **The model concedes** (`give_up`).
* **Budget** — step count or wall clock.
* **Policy** — an irreversible action with no human attending.
* **Replay** — a hard failure, or a recoverable condition past its retry budget.

The intervention packet has to be actionable without the conversation that
produced it: capability, goal, tenant, step index and intent, the reason, the
URL, a rendered observation, a screenshot, and an explicit statement of what the
operator is being asked to accomplish.

### Taking control of the live session

Control is a **single-valued lease over one session**, and it is *enforced*, not
documented. `LeasedSurface` refuses any write from a party that does not hold
the lease; reads stay open to both, since an operator watching while automation
works cannot damage anything. Raising an intervention moves the lease to `NONE`
— nobody is driving until a person claims it — and automation is refused
immediately. Claim → `grant_control` moves it to `OPERATOR`. Handback rotates
the token, so an operator who released the session cannot act with the token
they had, and a resumed run cannot act with one it captured before the handoff.
That is the failure this prevents: automation typing over a human mid-edit.

**A threading constraint turned out to be load-bearing rather than incidental.**
The browser driver is pinned to the thread that created it; the console is an
HTTP server on another. So the console never touches the surface — it *submits
commands*, and the automation thread, which is parked waiting for handback
anyway, pumps them. That inversion is the honest shape of the problem: the
automation must be the thing that yields, because it is the thing that has to
still be there afterwards to resume.

### Handing it back

`wait_for_handback` is the resume point. The engine parks there servicing
operator commands; when it returns, the lease is back with the agent under a
fresh token and the surface is wherever the human left it. If the operator asked
to resume, replay **re-enters the step loop at the step that stopped it** — same
session, same page, the state they left behind. Every operator action is
recorded into the intervention and the evidence log, redacted on the way to
disk. That record is not bookkeeping: it makes the handoff auditable, and it is
the raw material for noticing that the same manual fix keeps being needed and
belongs in the artifact.

**What is mocked:** the console's *presentation* — server-rendered HTML with a
poll-refreshed screenshot, not a co-browsing product. **What is not:** the
queue, the claim, the lease transfer, the actions, the handback, the resume and
the evidence trail. The scripted operator used to produce reproducible evidence
drives the same API from a worker thread, exactly as the HTML console does.

---

## 6. Safety

Three independent gates, because they fail for different reasons, plus
redaction. One `PolicyGate`, used identically by discovery and replay — a
guardrail only the exploratory path honours is not a guardrail, and one the
production path implements separately will drift from the one that was
reviewed.

**Reach — default-deny.** Origins and path prefixes are allowlisted; denied
patterns are checked first. Note that the target app's own fault injector
(`/admin`) is outside the allowlist *by design*: an agent that can inject faults
into the system it is driving can manufacture the evidence that it succeeded.

**Action kind.** A profile declares which of the nine actions are permitted. The
shipped `readonly` profile omits `select` and blocks irreversible acts outright.

**Risk.** Reads and navigations are `safe`; typing is `elevated` (nothing is
committed until a click); a click is classified from what the control is
*called*, against labels declared per product ("Open Account", "Post",
"Transfer", "Approve", "Disburse", "Wire"). Irreversible acts default to
`confirm` rather than `block`: blocking outright would rule out the flows
institutions most want automated. So the same capability behaves differently by
context — discovery is attended by definition and proceeds; unattended replay
escalates to a human. Additionally, an artifact containing an irreversible step
will not run unattended unless it is `approved`.

**Data.** Nothing sensitive is ever written down. Steps carry references, not
values, so a credential cannot reach an artifact even by accident. The same
holds in the other direction, which is easy to miss: the model emits
`@secret:MCB_PASSWORD` rather than a password, so the resolved value must not
come back to it as that field's *contents* on the next turn — it would have the
secret anyway, one step later and with no audit trail. Injected values are
therefore always scrubbed from the model's view, which still shows the field as
`<filled>`.

Content the *application* displays is a separate question. A member id is
printed on the detail screen and the model has to read that screen to operate,
so it is not redacted by default — justified only because the model is local
and nothing leaves the host. The `hosted` profile sets `redact_before_model`
and scrubs it, and that is the profile to use with any remote provider. Redaction is
enforced at a *single* write boundary (`EvidenceWriter.event`) rather than at
every call site, because a rule that depends on every caller remembering it is
not a rule; it covers regex classes (SSN, card, routing, email) and literal
values registered at runtime, which is what catches a password that looks like
ordinary text. Tests assert that neither the artifact nor the run log contains
the password, the operator id or the member id.

### Limits, stated plainly

* **Screenshots contain PII by construction.** A screenshot of an account detail
  screen is regulated data. Pattern matching cannot fix that; it is a question
  of where evidence is stored and who can read it, and this repo writes to a
  local directory with no retention policy. In production this is an encrypted
  store with expiry and access control.
* **Risk classification is label-based and therefore a heuristic.** A commit
  button labelled "Continue" would be classified `elevated`. The label list is
  deliberately over-broad — a false positive costs one confirmation, a false
  negative opens an account — but it is not a substitute for the vendor telling
  you which screens commit.
* **Redaction is not DLP.** It catches known classes and known values, not an
  unanticipated identifier format on an unfamiliar screen.
* **Policy cannot stop a correctly-approved capability doing the wrong thing.**
  Approval gates *whether* an irreversible capability runs unattended; it does
  not verify the flow was recorded against the right screen. That is what
  review, the stability record and the drift signal are for.
* **The allowlist is enforced in-process.** A compromised driver could bypass
  it; a production deployment would enforce egress at the network layer too.

---

## 7. Cuts

Cut deliberately, with the seam left real:

* **Desktop surface.** The `Surface` protocol, `UINode`, the pure resolver and
  the pure condition evaluator exist precisely so this is a driver, not a
  redesign. Not implemented — the brief asks for one concrete surface.
* **Real co-browsing console.** Presentation is mocked (§5); the mechanism is not.
* **Bounded LLM recovery on replay failure.** Tempting, and I left it out on
  purpose: it puts a model back into the production path, which is the one thing
  the whole design is arranged to avoid. Doing it responsibly needs a policy
  story I would rather build deliberately — a single step, inside the existing
  allowlist, never an irreversible one, recorded as evidence and requiring
  re-approval before it becomes part of the artifact.
* **Automatic overlay generation and route canonicalisation.** Both are
  mechanical given what is already recorded; neither is built.
* **Queues, workers, multi-tenant plumbing.** Infrastructure without a load.
* **Session reuse across invocations.** Every run signs on afresh, which is slow
  and unrealistic at volume. A session pool keyed by tenant + operator is the
  obvious next step and interacts with `SESSION_EXPIRED` handling.

### What I would build next, in order

1. **A second capability with an irreversible step** (opening a sub-account,
   which the target app already supports end to end) to exercise the approval
   gate and the confirm-on-irreversible path through a real flow rather than a
   unit test.
2. **Session pooling**, because sign-on dominates every replay and the
   expiry path is already modelled.
3. **Overlay generation**: record a capability on tenant A, dry-run its locators
   against tenant B, and emit the diff as a candidate overlay for review. All
   the inputs exist — this is the highest-leverage unbuilt thing here.
4. **Bounded single-step LLM recovery**, with the policy design above.
5. **Evidence storage with retention and access control**, which is the gap
   between this being demonstrably safe and being deployable.
