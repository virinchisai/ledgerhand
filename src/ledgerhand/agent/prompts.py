"""Prompt construction and decision validation.

Two constraints shaped this, and they pull the same way:

  * A 7B model on a CPU is slow, and prompt processing dominates. Every token
    in the observation costs wall-clock time on every step.
  * A small model follows a small, closed instruction set far better than an
    open one.

So the observation is compressed hard and the action space is tiny. The model
is never asked to produce a selector, a URL, or a value -- only to pick a
handle from a list and name a reference. Everything it could get subtly wrong
has been moved out of its reach.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from ..models.observation import Observation, UINode
from ..replay.locators import normalize

#: The action menu is assembled per turn from what is on screen. Offering
#: `select` on a screen with no dropdown is how a small model ends up trying to
#: "select" a table cell -- which is exactly what happened on the first real run.
_ACTION_FORMS = {
    "click": '{"action":"click","target":"<handle>","reason":"<short>"}',
    "type": '{"action":"type","target":"<handle>","value":"<text>","reason":"<short>"}',
    "select": '{"action":"select","target":"<handle>","value":"<option label>","reason":"<short>"}',
    "press": '{"action":"press","key":"Enter","reason":"<short>"}',
    "finish": '{"action":"finish","outputs":{"<output name>":"<handle>"},"reason":"<short>"}',
    "give_up": '{"action":"give_up","reason":"<why you cannot proceed>"}',
}

_RULES = """RULES:
1. "target" must be a handle copied from CONTROLS (or from READABLE, for finish outputs).
2. Never invent a handle, a URL, or a value.
3. To fill a field with a provided input, set "value" to its reference exactly,
   e.g. "@param:member_id". Do not guess the real value; you are not shown it.
4. For passwords use the secret reference exactly, e.g. "@secret:MCB_PASSWORD".
5. The moment the screen shows what OUTPUTS REQUIRED asks for, use "finish" and
   map every output name to a handle from READABLE. Do not act further.
6. You can only act on CONTROLS. Entries in READABLE are values to read, never
   things to click, type into or select.
7. A control showing current=<filled> is already done. Check WHAT YOU HAVE DONE
   SO FAR, and once every field the step needs is filled, click the button that
   submits the form.
8. If an error, dialog or notice is blocking you, deal with that first.
9. Keep "reason" under 8 words, and describe THIS action, not the last one.
10. Reply with ONE JSON object. No prose, no markdown fence.

EXAMPLE of finishing, when the goal asked for a balance and a name:
{"action":"finish","outputs":{"savings_balance":"f1_t14","member_name":"f1_t4"},"reason":"detail screen shows both"}"""


def build_system(available: list[str]) -> str:
    """System prompt for this turn, listing only the applicable actions."""
    forms = "\n".join(_ACTION_FORMS[a] for a in available if a in _ACTION_FORMS)
    return (
        "You operate a back-office banking application through a screen-reader view.\n"
        "Each turn you see the current screen and choose exactly ONE action.\n\n"
        f"ACTIONS (reply with one of these JSON shapes, nothing else):\n{forms}\n\n"
        f"{_RULES}"
    )


#: Full menu, for callers that do not vary it (tests, documentation).
SYSTEM = build_system(list(_ACTION_FORMS))


#: Below this many readable nodes a screen is a form, not a result. Asking
#: "are we done" there costs minutes of CPU inference for a certain "no".
GOAL_CHECK_MIN_READABLE = 8


def worth_goal_check(obs: "Observation") -> bool:
    return len([n for n in obs.controls if not _is_actionable(n) and n.text]) >= GOAL_CHECK_MIN_READABLE


def applicable_actions(obs: "Observation", *, include_finish: bool = True) -> list[str]:
    available = ["click", "give_up"] if not include_finish else ["click", "finish", "give_up"]
    roles = {n.role for n in obs.controls}
    if roles & {"textbox", "searchbox"}:
        available.insert(1, "type")
    if roles & {"combobox", "listbox"}:
        available.insert(2, "select")
    if roles & {"textbox", "searchbox"}:
        available.append("press")
    return available


class AgentDecision(BaseModel):
    """Validated model output. Anything that does not fit is rejected and retried."""
    action: str
    target: str | None = None
    value: str | None = None
    key: str | None = None
    outputs: dict[str, str] = Field(default_factory=dict)
    reason: str = ""

    @field_validator("action")
    @classmethod
    def _known(cls, v: str) -> str:
        allowed = {"click", "type", "select", "press", "finish", "give_up"}
        v = (v or "").strip().lower()
        if v not in allowed:
            raise ValueError(f"unknown action {v!r}; expected one of {sorted(allowed)}")
        return v

    @field_validator("target", mode="before")
    @classmethod
    def _clean_handle(cls, v: object) -> object:
        """Accept the handle as rendered.

        The prompt shows controls as `[f1_e2]`, and a small model copies what it
        sees, brackets and all. Rejecting that would be technically correct and
        practically useless -- the model identified the right control. Normalise
        instead, and keep the strictness for things that actually matter.
        """
        if isinstance(v, str):
            return v.strip().strip("[]").strip()
        return v

    @field_validator("outputs", mode="before")
    @classmethod
    def _coerce_outputs(cls, v: Any) -> Any:
        if not isinstance(v, dict):
            return {}
        return {k: (x.strip().strip("[]").strip() if isinstance(x, str) else x)
                for k, x in v.items()}

    def describe(self) -> str:
        bits = [self.action]
        if self.target:
            bits.append(self.target)
        if self.value:
            bits.append(f"={self.value!r}")
        if self.key:
            bits.append(f"key={self.key}")
        if self.outputs:
            bits.append(f"outputs={self.outputs}")
        return " ".join(bits)


def _is_actionable(node: UINode) -> bool:
    return node.role in {"textbox", "button", "combobox", "listbox", "checkbox",
                         "radio", "link", "menuitem"}


def render_controls(obs: Observation, limit: int = 40) -> str:
    rows = [n for n in obs.controls if _is_actionable(n)][:limit]
    if not rows:
        return "  (none)"
    out = []
    for n in rows:
        line = f"  [{n.handle}] {n.role} \"{n.label[:52]}\""
        if n.role in ("combobox", "listbox") and n.attrs.get("options"):
            line += f" options={n.attrs['options'][:70]}"
        if n.value and n.value != n.label:
            # A redacted value still tells the model the field is filled, which
            # is what it needs to decide whether to move on to the submit.
            shown = "<filled>" if n.value.startswith("<redacted:") else n.value[:28]
            line += f" current={shown!r}"
        if not n.enabled:
            line += " (disabled)"
        out.append(line)
    return "\n".join(out)


def _sample_grid(nodes: list[UINode], budget: int) -> list[UINode]:
    """Show a representative slice of a grid, not one column three times over.

    Ranking grid cells by length puts every `ACTIVE` in the Status column ahead
    of the balance the caller asked for. Taking a round-robin across columns --
    after collapsing repeated values within a column, since a third identical
    `ACTIVE` says nothing the first did not -- spends the budget on breadth,
    which is what makes an output findable.
    """
    by_column: dict[str, list[UINode]] = {}
    for node in sorted(nodes, key=lambda n: len(n.text)):
        column = by_column.setdefault(node.attrs.get("col_header", ""), [])
        if any(normalize(existing.text) == normalize(node.text) for existing in column):
            continue
        column.append(node)
    out: list[UINode] = []
    depth = 0
    while len(out) < budget and any(len(c) > depth for c in by_column.values()):
        for column in by_column.values():
            if depth < len(column) and len(out) < budget:
                out.append(column[depth])
        depth += 1
    return out


def _value_first(node: UINode) -> tuple[int, int]:
    """Order loose text so values beat the labels that name them.

    In a two-column layout table -- which is how every one of these detail
    screens is built -- the leading cell names the field and the rest hold the
    data. `Member`, `Branch`, `Status` are labels; the member's name is the
    answer. Ranking by length alone puts the labels first, and on a screen with
    a busy grid that pushes the actual value off the end of the budget, making
    an output the caller asked for impossible to bind.
    """
    index = node.attrs.get("col_index")
    is_label = index in (None, "", "0")
    return (1 if is_label else 0, len(node.text))


def render_readable(obs: Observation, limit: int = 18) -> str:
    """Extraction candidates: values the model can bind an output name to.

    Two subtleties, both learned from real runs:

    * A table's own header cells are readable text but are never the answer, so
      they are dropped -- they would otherwise consume a third of the budget.
    * Naive sorting is a trap. On a member detail screen the accounts grid
      supplies a dozen cells that all carry column headers, and sorting by "has
      a column header" pushes the member's *name* off the end of the list --
      making an output the model was asked for impossible to bind. So the
      budget is split: most of it to grid cells, a guaranteed share to
      everything else.
    """
    rows = [n for n in obs.controls if not _is_actionable(n) and n.text]
    # A grid's header row is readable text and is never an answer. Its cells
    # are not recognisable from their own attributes -- a header cell has no
    # column header of its own -- so they are identified by being the strings
    # that other cells name as their column.
    headers = {normalize(n.attrs["col_header"]) for n in obs.controls
               if n.attrs.get("col_header")}
    rows = [n for n in rows if normalize(n.text) not in headers]
    in_grid = [n for n in rows if n.attrs.get("col_header")]
    loose = [n for n in rows if not n.attrs.get("col_header")]
    grid_budget = max(1, (limit * 2) // 3)
    chosen = (_sample_grid(in_grid, grid_budget)
              + sorted(loose, key=_value_first)[:limit - grid_budget])

    out = []
    for n in chosen:
        line = f'  [{n.handle}] {n.role} "{n.text[:44]}"'
        col, row = n.attrs.get("col_header"), n.attrs.get("row_text")
        if col:
            line += f" (column: {col}"
            if row:
                line += f", row: {row[:44]}"
            line += ")"
        out.append(line)
    return "\n".join(out) or "  (none)"


GOAL_CHECK_SYSTEM = """You read one screen and report where values are.
Reply with ONE JSON object mapping each requested name to a handle from VALUES
ON SCREEN, or to "none" if that value is not on this screen.
No prose, no extra keys, no markdown fence."""


def build_goal_check(goal: str, outputs: list[tuple[str, str]], readable: str) -> str:
    """The 'are we there yet' question, asked on its own.

    Deliberately carries no controls, no history and no action vocabulary. It is
    a reading-comprehension question about one screen, and keeping it that way
    is what makes it answerable by a small model in a fraction of the time the
    full decision costs.
    """
    wanted = "\n".join(f"  {name} -- {desc}" for name, desc in outputs)
    shape = ", ".join(f'"{name}":"<handle or none>"' for name, _ in outputs)
    return (f"GOAL: {goal}\n\nVALUES WANTED:\n{wanted}\n\n"
            f"VALUES ON SCREEN:\n{readable}\n\n"
            f"For each wanted value, give the handle that holds it, or \"none\".\n"
            f"Reply exactly: {{{shape}}}")


def build_user_prompt(
    *,
    goal: str,
    obs: Observation,
    params: list[tuple[str, str]],
    secrets: list[tuple[str, str]],
    outputs: list[tuple[str, str]],
    history: list[str],
    screen_text_limit: int = 10,
    include_readable: bool = True,
) -> str:
    lines = [f"GOAL: {goal}", ""]
    if params:
        lines.append("INPUTS AVAILABLE (use the reference, never a literal):")
        lines += [f'  @param:{n} -- {d}' for n, d in params]
    if secrets:
        lines.append("SECRETS AVAILABLE (use the reference, never a literal):")
        lines += [f"  @secret:{name}" + (f" -- {desc}" if desc else "")
                  for name, desc in secrets]
    if outputs:
        lines.append("OUTPUTS REQUIRED before you may finish:")
        lines += [f"  {n} -- {d}" for n, d in outputs]
    if history:
        lines.append("")
        lines.append("WHAT YOU HAVE DONE SO FAR:")
        lines += [f"  {h}" for h in history[-5:]]
    lines += ["", "SCREEN", f"  url: {obs.url}", f"  title: {obs.title}"]
    if obs.content_url and obs.content_url != obs.url:
        lines.append(f"  content frame: {obs.content_url}")
    if obs.transport_error:
        lines.append(f"  TRANSPORT ERROR: {obs.transport_error}")
    lines.append("TEXT:")
    lines += [f"  {t[:90]}" for t in obs.texts[:screen_text_limit]]
    lines.append("CONTROLS:")
    lines.append(render_controls(obs))
    if include_readable:
        lines += ["READABLE:", render_readable(obs)]
    # The decision cue goes last, immediately before the question. A small
    # model weights the end of a long prompt far more than the middle, and this
    # instruction is the one it was getting wrong: standing on the screen that
    # held the answer and navigating away from it.
    if outputs and include_readable:
        names = ", ".join(n for n, _ in outputs)
        lines += ["", f"BEFORE YOU ACT: this goal needs {names}.",
                  "If those values are visible in READABLE above, reply with "
                  '"finish" and map each name to its handle. Do not navigate away.',
                  "Otherwise choose one action that gets closer."]
    elif outputs:
        names = ", ".join(n for n, _ in outputs)
        lines += ["", f"You are working towards: {names}. "
                      "Choose the action that gets closer to a screen showing them."]
    lines += ["", "Choose the single next action. Reply with one JSON object."]
    return "\n".join(lines)
