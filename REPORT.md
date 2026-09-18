# Ledgerhand — design write-up

> **The model discovers. The artifact is the capability. Deterministic replay is
> how the agent invokes it in production.**

A model runs once per capability, during discovery. Everything after is a typed
contract and an executor. The engineering is in what the loop may emit, and in
what the executor does when the screen is not what was recorded.

## 1. Architecture

```
goal.yaml ─▶ DiscoveryAgent ─▶ Recorder ─▶ CapabilityArtifact (JSON)
                  │                               │
                  ▼        same protocol          ▼
            Surface  ◀──────────────────── ReplayEngine (no model)
                  │                               │
           SessionBroker ── control lease ── operator console
                          PolicyGate · Redactor
```

`Surface` answers *what is on screen* (`UINode`: role, accessible name, value,
state, geometry, frame path) and *do one semantic action to one of those nodes* —
no selectors. `Recorder` is a compiler, not a serialiser. `ReplayEngine` imports
no LLM client. `PolicyGate` and `Redactor` serve both paths identically.

**Perception is role-and-name-first, computed in-page.** I began with Chrome's
accessibility tree over CDP — the right *shape*, insufficient here: a text box
whose label is a sibling `<td>` has an **empty accessible name**. I verified that
before building anything else. Recovering the label from layout is the job, and
it must happen where the layout is. So `perception.js` computes an AX-shaped
projection: the ARIA rules browsers agree on, then legacy fallbacks (the cell to
the left, preceding text, `ctl00$…$txtMemberId` de-camelised). The output shape
is what macOS AX and Windows UIA already give you, which makes a desktop driver a
producer swap rather than a redesign. Acting is real mouse and keyboard input at
the control's position, not `element.click()` — except a native `<select>`, which
has no affordance a mouse can drive, so the driver uses the widget API as a
desktop driver would use `AXSetValue`.

**The model is local** (`qwen2.5:7b-instruct` via Ollama). Discovery reads live
back-office screens; locally, member names and balances never leave the host. And
a 7B at ~1.4 tok/s is a *harder* test: if the loop completes a real flow under
that, the loop is carrying the weight. From `/evidence`: **6 decisions, 21
minutes**; the replay taken from it runs in **9.8 seconds with no model**. That
ratio is the argument for record-once/replay-many, and for why the production
path must not contain a model. Provider is a seam, not an assumption.

**The target is purpose-built, not a public demo site.** An error taxonomy
separating business outcomes from failures, and reuse across tenants, both need
*injectable* faults and *two* tenants of one product. No public site offers
either, and automating one adds ToS risk for nothing. The cost is real — a mock
cannot prove robustness against the diversity of real markup — but it is what
makes every claim below testable.

**The loop asks two questions, not one.** This is what made the run succeed, and
it began as a bug. Originally the loop asked "what next?" with `finish` on the
menu, fusing *what do I do* with *am I done, and where are the values*. The
second is reading comprehension over one screen, needing no history, controls or
action vocabulary. Fused, the detail screen produced a ~900-token prompt this
model timed out on at 900s and, earlier, answered by clicking "New Search" —
walking off the screen holding the answer. Split, the goal check is ~170 tokens
and binds both outputs in one shot, gated on the screen showing data (≥8 readable
nodes) because an extra call costs minutes. Same lesson, smaller: the action menu
is assembled from what is on screen (offered `select` unconditionally, the model
tried to "select SAVINGS" on a table cell). When a small model errs, first ask
whether the loop gave it the chance to.

**One process, synchronous.** The unit of work is one live browser session —
stateful and long-lived; distributing it means shipping a session between
workers. Scaling is "more session workers", and the seam that needs (`catalog` /
`invoke` by name) already exists.

## 2. Artifact schema

**A capability is an API contract that happens to be implemented by driving a
UI** — typed inputs, typed outputs, declared outcomes, a version. Not a macro
recording; the step list is an implementation detail.

```
CapabilityArtifact
  schema_version · id · version · name · description
  target      product · product_version · tenant · surface · entry_url
  inputs      [ParamSpec]   type · required · sensitivity · pattern/enum/range
  outputs     [OutputSpec]  type · sensitivity · ExtractionSpec(control, transform)
  outcomes    [OutcomeSpec] code · classification · detector · recovery
  steps       [Step]        action · ControlDescriptor · ValueRef · wait ·
                            checkpoint · risk · optional
  success_condition · policy · provenance · approval · stability · overlays
```

**Values are references, never literals.** `@secret:MCB_PASSWORD` and
`@param:member_id` carry a name; the value resolves at invocation. This holds in
both directions, which is easy to miss: the model emits those references and is
never shown a value — it completed a real member lookup without seeing a member
number — and the resolved value must not return to it as that field's contents
next turn, or it has the secret anyway, one step later, with no audit trail.
Injected values are scrubbed from the model's view; the field still reads
`<filled>`. Tests assert the run's password, operator id and member id appear
nowhere in the artifact or the prompts.

**Targeting is a ranked bundle of hypotheses** — `ax_role_name → label_text →
anchor_relative → dom_attr → dom_css → ordinal` — encoding the bet that *what a
control is and what it is called* outlives *where it sits in the markup*.
Crucially the ranking is not fixed per kind: at record time each candidate is
tested against the observation it was recorded in and kept only if it uniquely
identifies the node, with confidence from that test. A strategy already ambiguous
at record time would certainly be ambiguous at replay. Where only position
identifies a control, `ordinal` is recorded honestly at confidence 0.2 so review
catches it.

**Outcomes are declared, and product-level.** This is the honest answer to "how
did you discover the error paths?" — you cannot, from a successful run. So
`products/meridian-core.yaml` declares them once: `MCB-0042` means no such
member, on every screen, in every institution running Meridian Core 7.x. A new
capability inherits handling for paths its discovery never visited; fixing a
detector fixes it everywhere; a tenant with different wording overrides one
detector, not a flow. **Provenance excludes the transcript** (kept in
`evidence/`): the artifact stays reviewable, and model chatter quotes screen
contents — regulated data — which must not enter a committed document.
`tool_schema()` publishes the capability as a function-calling definition, so
`catalog --as-tools` emits a catalog an agent can invoke by name.

## 3. Determinism & error handling

**Levers.** No model in `replay/`. The resolver and condition evaluator are pure
functions over an `Observation` — every locator, ambiguity and drift case is
unit-tested without launching anything, and the same code serves a desktop
driver. Waits are conditions, not clocks. Resolution requires a *unique* match;
two is an ambiguity, reported, never guessed. Arguments are validated before the
surface is touched, so "you passed a letter where a member id goes" does not
arrive disguised as a validation error from the bank. Checkpoints attach where
the screen changed — typing steps carry none, which is correct: typing changes no
observable state, so an assertion there would be vacuous.

Two recorder bugs are worth naming; both produce artifacts that pass their own
replay and fail on the second invocation, and both were caught by tests that
replay with *different* arguments. **Anchoring on an identifier**: in
`0001-4471 | SAVINGS | ACTIVE | 4,182.55` the most distinctive cell is the
account number, and it is per-record — the recorder now prefers categorical cells
and anchors on `SAVINGS`. **Checkpointing on record data**: the detail screen
surfaces both `Member Detail` and `MAIN`, the latter being member 12345's branch
— derivation now rejects parameter values, anything with a digit, anything
rendered as a control's value, and generic chrome, then scores on casing and
position. That scoring is the weakest link and is backed rather than trusted: the
success condition independently requires every output control to resolve, and the
checkpoint is written in plain text where a reviewer sees it before approval.

| Class | Meaning | Result |
|---|---|---|
| `business_outcome` | A legitimate answer the caller asked for | `status=business_outcome`, `outcome_code`, `ok == True` |
| `recoverable` | Clearable; the run continues | recovery runs, step retried, recorded |
| `hard_failure` | Stop and surface it | `status=failed` + `FailureDetail` + screenshot + DOM snapshot |

**The ordering is the most important line in the engine.** Declared outcomes are
evaluated *before* the checkpoint. A not-found screen fails the checkpoint too,
so checking that first would report "expected `Member Detail`, observed something
else" for what is a correct answer. `ReplayResult.ok` is true for `success` **and**
`business_outcome`, distinct from `status is SUCCESS`, which also means the
outputs came back. Callers branch on a code, never parse a message.

In `/evidence`: `99999` → `MEMBER_NOT_FOUND`; `55555` → `ACCESS_DENIED`; injected
interstitial → cleared and recorded, run completes; session expiry →
`SESSION_EXPIRED` hard failure with failing step, expected-vs-observed,
screenshot and markup dump; HTTP 500 → hard failure; 1.2s latency → absorbed; bad
argument → rejected before any action. Session expiry is deliberately *not*
recoverable: re-authenticating mid-flow would silently resume a run whose earlier
steps may have half-committed, using credentials replay is not given.

**Drift** is secondary — these UIs change slowly — but recorded: replay notes
*which* hypothesis resolved each control, anything but the primary sets
`drift_detected` and accumulates into `StabilityRecord`. So a renamed field is
reported the first time it happens while the run still completes.

## 4. Heterogeneity & multi-tenant

The seam is `Surface`. A `UINode` carries only what macOS AX and Windows UIA also
expose, and everything above is already surface-independent: the schema names
roles and accessible names, resolver and evaluator are pure functions over
`UINode`, outcomes are text and control assertions, policy reasons about action
kinds and labels. A **desktop driver** implements the protocol and nothing else
changes — `perceive()` walks the AX/UIA tree (which computes accessible names for
you), `act()` uses `AXPress`/`AXSetValue` or synthesised input at the element's
screen rect, `frame_path` becomes a window path. Only `dom_attr` and `dom_css` do
not carry over, which is the point of ranking them last. A surface with no
element model at all (a terminal, a Citrix pixel stream) would genuinely need
work: OCR plus coordinates, `box` as the only handle, `ordinal` no longer a last
resort. The schema survives; confidence does not.

**Reuse across tenants**, three mechanisms, no per-tenant rebuilds. (1) The
**product profile** owns the error taxonomy, recovery flows and risk labels —
they belong to `meridian-core`, not to a capability or tenant. (2) **Base +
`TenantOverlay`**: a reviewable diff of control, URL and detector overrides;
`resolve_for(tenant)` returns a specialised artifact so the engine never knows
overlays exist. Tenant B renames `Member ID` → `Member Number`, `Search` →
`Find`, `/search` → `/lookup` and runs build 7.4.03 — the same artifact replays
against it through **three overrides**: one entry URL and two control labels
(`overlays/summitcu.yaml`, evidence row 10). Overlays are
authored against control labels (`step:Member ID`) and resolved to indices on
apply, so a re-recording cannot silently renumber them. (3) **Normalisation**
folds case, whitespace, non-breaking spaces and trailing punctuation, so much
cross-tenant difference never needs an overlay at all.

**Detecting drift** falls out of the above: a tenant whose capability starts
resolving by `dom_attr` instead of `label_text` is telling you its labels
changed — per tenant, per run, with nobody writing a drift detector. Not built:
automatic overlay generation from a two-tenant diff, route canonicalisation
(`/item/12345` → `/item/:id`), version-range enforcement.

## 5. Escalation & handoff

**Detecting stuck** — six triggers, because the causes differ: **no progress** (a
digest of URL + control labels unchanged across four steps — the honest
definition, and what catches a loop happily acting and going nowhere); unusable
model output twice running; the model conceding; budget; policy (an irreversible
action unattended); and in replay, a hard failure or a recoverable condition past
its retry budget. The intervention packet must be actionable without the
conversation that produced it: capability, goal, tenant, step index and intent,
reason, URL, a rendered observation, a screenshot, and what the operator is asked
to accomplish.

**Taking control.** Control is a single-valued lease over one session, and it is
*enforced*: `LeasedSurface` refuses any write from a non-holder. Reads stay open
to both — an operator watching while automation works damages nothing. Raising an
intervention moves the lease to `NONE` (nobody drives until a person claims it)
and automation is refused immediately. Handback **rotates the token**, so a
released operator cannot act and a resumed run cannot act with a token captured
before the handoff. That is the failure this prevents: automation typing over a
human mid-edit.

**A threading constraint turned out to be load-bearing.** The browser driver is
pinned to its creating thread; the console is an HTTP server on another. So the
console never touches the surface — it submits commands, and the automation
thread, parked waiting for handback anyway, pumps them. That inversion is the
honest shape: the automation must be the thing that yields, because it is the
thing that has to still be there to resume. When `wait_for_handback` returns, the
lease is back under a fresh token and the surface is where the human left it; if
they asked to resume, replay re-enters the step loop **at the step that stopped
it**. Every operator action is recorded and redacted on the way to disk — which
makes the handoff auditable, and is the raw material for noticing that the same
manual fix keeps being needed and belongs in the artifact.

**Mocked:** the console's *presentation* — server-rendered HTML with a
poll-refreshed screenshot, not a co-browsing product. **Not mocked:** the queue,
claim, lease transfer, actions, handback, resume and evidence trail. The scripted
operator used for reproducible evidence drives the same API from a worker thread,
exactly as the HTML console does.

## 6. Safety

Three gates plus redaction, in one `PolicyGate` used identically by discovery and
replay — a guardrail only the exploratory path honours is not a guardrail, and
one the production path reimplements will drift from the one that was reviewed.

**Reach** is default-deny on origins and path prefixes. The target's own fault
injector (`/admin`) is outside the allowlist *by design*: an agent that can
inject faults into the system it drives can manufacture the evidence that it
succeeded. **Action kind**: a profile declares which of the nine actions are
permitted; the shipped `readonly` profile omits `select` and blocks irreversible
acts. **Risk**: reads and navigations are `safe`, typing is `elevated` (nothing
commits until a click), and a click is classified from what the control is
*called*, against labels declared per product. Irreversible acts default to
`confirm`, not `block` — blocking would rule out the flows institutions most want
automated. So the same capability behaves differently by context: discovery is
attended by definition and proceeds; unattended replay escalates. An artifact
containing an irreversible step will not run unattended unless `approved` — and
approval is what satisfies the per-step gate too, otherwise it would gate entry
to a run that then escalates at the very step approval was granted for.
`member.subaccount.open`, whose last click opens an account, demonstrates the
whole ladder in `/evidence`: draft+unattended refused before touching the UI,
attended succeeds, approved+unattended succeeds, revoked refused.

**Data.** Steps carry references, not values, and the same holds in reverse
(§2). Content the *application* displays is a separate question: a member id is
printed on the detail screen and the model must read that screen, so it is not
redacted by default — justified only because the model is local. The `hosted`
profile sets `redact_before_model` and scrubs screen text *and URLs*
(`/members/detail/12345` is exactly how identifiers leak into places nobody
looks). Redaction is enforced at a single write boundary, not at every call site,
because a rule depending on every caller remembering it is not a rule.

**Limits.** Screenshots contain PII by construction — a question of where
evidence is stored and who can read it, and this repo writes locally with no
retention policy. Risk classification is label-based and heuristic: a commit
button labelled "Continue" classifies as `elevated`. The list is deliberately
over-broad — a false positive costs one confirmation, a false negative opens an
account — but is no substitute for the vendor telling you which screens commit.
Redaction catches known classes and known values, not an unanticipated identifier
format. Approval gates *whether* an irreversible capability runs unattended, not
whether it was recorded against the right screen; review, the stability record
and the drift signal are for that. The allowlist is enforced in-process;
production would enforce egress at the network layer too.

## 7. Cuts

Cut deliberately, seam left real: the **desktop surface** (the protocol,
`UINode` and the pure resolver exist so this is a driver, not a redesign); a
**real co-browsing console** (presentation mocked, mechanism not); **overlay
generation and route canonicalisation** (mechanical given what is recorded);
**queues and workers** (infrastructure without a load); and **session reuse
across invocations** (every run signs on afresh — slow and unrealistic at
volume). **Bounded LLM recovery on replay failure** I left out on purpose: it
puts a model back in the production path, the one thing the design is arranged to
avoid. Doing it responsibly needs a policy story I would rather build
deliberately — a single step, inside the allowlist, never irreversible, recorded
as evidence, requiring re-approval to enter the artifact.

**One limitation worth stating rather than burying.** The discovery loop
completed the six-step lookup flow on a local 7B, repeatedly. It did *not*
complete the eleven-step account-opening flow: four attempts, each dying at a
different late step — a 900s timeout on the largest prompt, a dropdown reported
by its value attribute rather than its label so the model re-selected in a loop,
and finally oscillating between two dropdown states with the submit button on
screen. Each of those was a real defect and each is fixed, but the honest
reading is that this loop's ceiling on a 7B at ~1.4 tok/s is somewhere around
six to eight decisions, not eleven. A frontier model would very likely walk it;
the point of using a weak one was to find exactly this kind of edge, and the
failed run is kept in `/evidence` rather than deleted. So
`member.subaccount.open` was recorded by walking the flow deterministically
(`scripts/record_reference_capability.py`), its provenance says so, and it earns
its place by exercising the replay-side guardrails rather than by pretending to
be a discovery run.

**Next, in order.** (1) Re-attempt that flow on a stronger model, which the
provider seam already supports, to confirm the ceiling is the model rather than
the loop. (2) Session pooling:
sign-on dominates every replay and the expiry path is already modelled. (3)
Overlay generation: record on tenant A, dry-run its locators against tenant B,
emit the diff for review — every input exists, and it is the highest-leverage
unbuilt thing here. (4) Bounded single-step LLM recovery, with the policy design
above. (5) Evidence storage with retention and access control — the gap between
demonstrably safe and deployable.
