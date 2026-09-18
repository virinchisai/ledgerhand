"""Shared vocabulary. Kept in one place because the agent, the artifact and the
replay engine must agree on these exactly -- they are the system's type system."""
from __future__ import annotations

from enum import Enum


class SurfaceKind(str, Enum):
    """The class of surface a capability was recorded against.

    The replay engine is written against the Surface protocol, not against any
    one of these -- adding DESKTOP means adding a driver, not changing artifacts.
    """
    WEB = "web"
    DESKTOP = "desktop"
    TERMINAL = "terminal"


class ActionKind(str, Enum):
    """The closed action vocabulary.

    Deliberately small. A small vocabulary is what lets a 7B model drive the
    loop reliably, and it is what makes replay auditable -- a reviewer can read
    an artifact and know exactly what it is able to do.
    """
    NAVIGATE = "navigate"
    CLICK = "click"
    TYPE = "type"
    SELECT = "select"
    PRESS = "press"
    EXTRACT = "extract"
    WAIT = "wait"
    ASSERT = "assert"
    FINISH = "finish"


#: Actions that only read state. Everything else mutates something.
READ_ONLY_ACTIONS = frozenset({ActionKind.EXTRACT, ActionKind.WAIT, ActionKind.ASSERT})


class RiskTier(str, Enum):
    """How much damage a step can do if it fires wrongly.

    SAFE          -- reads, navigations within the allowlist. Replayable unattended.
    ELEVATED      -- writes that are reversible or scoped (filling a form field).
    IRREVERSIBLE  -- submits money movement / record creation / anything a human
                     would have to phone someone to undo.
    """
    SAFE = "safe"
    ELEVATED = "elevated"
    IRREVERSIBLE = "irreversible"


class Sensitivity(str, Enum):
    """Data classification. Drives redaction in artifacts, logs and evidence."""
    PUBLIC = "public"
    INTERNAL = "internal"
    PII = "pii"
    SECRET = "secret"


#: Never written to disk in the clear, anywhere.
REDACTED_CLASSES = frozenset({Sensitivity.PII, Sensitivity.SECRET})


class ParamType(str, Enum):
    STRING = "string"
    INTEGER = "integer"
    NUMBER = "number"
    BOOLEAN = "boolean"
    ENUM = "enum"
    MONEY = "money"


class LocatorKind(str, Enum):
    """Locator strategies, listed best-first.

    The ordering encodes the core bet of this system: on legacy enterprise UIs,
    *what a control is and what it is called* survives far longer than *where it
    sits in the markup*. DOM strategies are recorded, but they are the fallback,
    not the plan.
    """
    AX_ROLE_NAME = "ax_role_name"        # role + accessible name. Portable to desktop AX/UIA.
    LABEL_TEXT = "label_text"            # visible label inferred from layout (legacy tables).
    ANCHOR_RELATIVE = "anchor_relative"  # "the cell in the row whose type is SAVINGS".
    DOM_ATTR = "dom_attr"                # stable-ish generated name attr (ctl00$...).
    DOM_CSS = "dom_css"                  # last resort; brittle by construction.
    ORDINAL = "ordinal"                  # nth control of a role in a region. Flagged low-confidence.


class OutcomeClass(str, Enum):
    """The error taxonomy. This distinction is the point of the whole contract.

    BUSINESS   -- a legitimate answer the caller asked for ("no such member").
                  Not a crash. Returned as a successful call with an outcome code.
    RECOVERABLE-- the run can continue if we do something first (dismiss an
                  interstitial, wait out a slow load, re-auth).
    HARD       -- stop. Something is wrong that replay must not paper over.
    """
    BUSINESS = "business_outcome"
    RECOVERABLE = "recoverable"
    HARD = "hard_failure"


class ReplayStatus(str, Enum):
    SUCCESS = "success"
    BUSINESS_OUTCOME = "business_outcome"
    FAILED = "failed"
    ESCALATED = "escalated"


class ApprovalState(str, Enum):
    """Unattended replay is gated on this."""
    DRAFT = "draft"
    APPROVED = "approved"
    REVOKED = "revoked"


class ControlOwner(str, Enum):
    """Who holds the lease on a live session. Exactly one owner at a time."""
    AGENT = "agent"
    OPERATOR = "operator"
    NONE = "none"
