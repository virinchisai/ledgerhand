"""Session brokering: who is driving, and how control changes hands.

The requirement is that a human takes over the *same live session* the
automation was using, does something, and hands it back. That makes "who is in
control" a real piece of state, so it is modelled as a single-valued lease over
one session, and writes are refused to anyone who is not the current holder.
Refusing is the part that matters: a control model that is only documented is
one resumed-too-early automation away from typing over an operator's work.

There is a second constraint that turns out to be load-bearing rather than
incidental. The browser driver is single-threaded and pinned to the thread that
created it, while the operator console is an HTTP server on another thread. So
the console never touches the surface. It *submits commands*, and the
automation thread -- which is parked waiting for handback anyway -- pumps them.

That inversion is not a workaround. It is the honest shape of the problem: the
automation must be the thing that yields, because it is the thing that has to
still be there afterwards to resume.
"""
from __future__ import annotations

import queue
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from ..evidence.recorder import EvidenceWriter
from ..models.enums import ActionKind, ControlOwner
from ..models.intervention import (
    ControlLease, InterventionRequest, InterventionStatus, OperatorAction,
)
from ..models.observation import Observation
from ..surface.base import ActRequest, ActResult, Surface


class ControlError(RuntimeError):
    """Raised when something tries to act without holding the lease."""


@dataclass
class _Command:
    """One operator instruction, queued for the thread that owns the surface."""
    fn: Callable[[], Any]
    done: threading.Event = field(default_factory=threading.Event)
    result: Any = None
    error: Exception | None = None


class SessionBroker:
    """Owns one live session and the lease over it."""

    def __init__(self, surface: Surface, evidence: EvidenceWriter,
                 *, session_id: str | None = None) -> None:
        self.surface = surface
        self.evidence = evidence
        self.session_id = session_id or f"sess_{uuid.uuid4().hex[:8]}"
        self.lease = ControlLease(owner=ControlOwner.AGENT, holder_label="agent")
        self.interventions: dict[str, InterventionRequest] = {}
        self._commands: "queue.Queue[_Command]" = queue.Queue()
        self._lock = threading.RLock()

    # -- lease ---------------------------------------------------------------

    @property
    def agent_token(self) -> str:
        """The token automation must present. Rotates on every handback, so a
        resumed run cannot act with a token it captured before the handoff."""
        return self.lease.token

    def assert_control(self, owner: ControlOwner, token: str | None = None) -> None:
        with self._lock:
            if self.lease.owner is not owner:
                raise ControlError(
                    f"{owner.value} tried to act while control is held by "
                    f"{self.lease.owner.value}")
            if token is not None and token != self.lease.token:
                raise ControlError(f"{owner.value} presented a stale control token")

    def _transfer(self, owner: ControlOwner, label: str) -> ControlLease:
        with self._lock:
            previous = self.lease
            self.lease = previous.rotate(owner, label)
            self.evidence.event("control_transfer", session=self.session_id,
                                frm=previous.owner.value, to=owner.value, holder=label)
            return self.lease

    # -- intervention lifecycle ---------------------------------------------

    def raise_intervention(self, request: InterventionRequest) -> InterventionRequest:
        """Pause automation and put the request on the queue for a human.

        Control goes to NONE rather than straight to the operator: nobody is
        holding the session until a person actually claims it, and the
        automation must not be able to carry on in the meantime.
        """
        request.session_id = self.session_id
        with self._lock:
            self.interventions[request.id] = request
        self._transfer(ControlOwner.NONE, "unassigned")
        self.evidence.event("intervention_raised", intervention=request.id,
                            trigger=request.trigger.value, reason=request.reason,
                            capability=request.capability, step=request.step_index)
        self.evidence.write_json(f"intervention_{request.id}.json", request)
        return request

    def claim(self, intervention_id: str, operator: str) -> InterventionRequest:
        request = self._get(intervention_id)
        with self._lock:
            request.operator = operator
            request.claimed_at = datetime.now(timezone.utc)
            request.status = InterventionStatus.CLAIMED
        self.evidence.event("intervention_claimed", intervention=request.id, operator=operator)
        return request

    def grant_control(self, intervention_id: str) -> str:
        """Hand the live session to the operator. Returns their control token."""
        request = self._get(intervention_id)
        if request.operator is None:
            raise ControlError("intervention must be claimed before control is granted")
        lease = self._transfer(ControlOwner.OPERATOR, request.operator)
        request.status = InterventionStatus.OPERATOR_ACTIVE
        request.operator_actions.append(OperatorAction(
            kind="note", note=f"{request.operator} took control of session {self.session_id}"))
        return lease.token

    def handback(
        self, intervention_id: str, token: str, *, resume: bool, resolution: str = ""
    ) -> InterventionRequest:
        """Operator returns the session. Automation may continue from here."""
        self.assert_control(ControlOwner.OPERATOR, token)
        request = self._get(intervention_id)
        with self._lock:
            request.status = InterventionStatus.RETURNED
            request.returned_at = datetime.now(timezone.utc)
            request.resume_requested = resume
            request.resolution = resolution
            request.operator_actions.append(OperatorAction(
                kind="resume" if resume else "abort", note=resolution))
        self._transfer(ControlOwner.AGENT, "agent")
        self.evidence.event("intervention_returned", intervention=request.id,
                            resume=resume, resolution=resolution,
                            operator_actions=len(request.operator_actions))
        self.evidence.write_json(f"intervention_{request.id}.json", request)
        return request

    def resolve(self, intervention_id: str, note: str = "") -> InterventionRequest:
        request = self._get(intervention_id)
        request.status = InterventionStatus.RESOLVED
        request.resolution = note or request.resolution
        self.evidence.event("intervention_resolved", intervention=request.id, note=note)
        self.evidence.write_json(f"intervention_{request.id}.json", request)
        return request

    def _get(self, intervention_id: str) -> InterventionRequest:
        request = self.interventions.get(intervention_id)
        if request is None:
            raise KeyError(f"no such intervention: {intervention_id}")
        return request

    @property
    def pending(self) -> list[InterventionRequest]:
        return [r for r in self.interventions.values()
                if r.status in (InterventionStatus.PENDING, InterventionStatus.CLAIMED,
                                InterventionStatus.OPERATOR_ACTIVE)]

    # -- cross-thread command channel ---------------------------------------

    def submit(self, fn: Callable[[], Any], *, timeout: float = 30.0) -> Any:
        """Called from the console thread. Runs `fn` on the surface-owning
        thread and returns its result."""
        command = _Command(fn=fn)
        self._commands.put(command)
        if not command.done.wait(timeout):
            raise TimeoutError("the automation thread did not pump this command in time")
        if command.error is not None:
            raise command.error
        return command.result

    def pump(self, timeout: float = 0.2) -> bool:
        """Called from the surface-owning thread. Runs one queued command."""
        try:
            command = self._commands.get(timeout=timeout)
        except queue.Empty:
            return False
        try:
            command.result = command.fn()
        except Exception as exc:  # surfaced to the console caller
            command.error = exc
        finally:
            command.done.set()
        return True

    def wait_for_handback(
        self, intervention_id: str, *, timeout_s: float = 900.0
    ) -> InterventionRequest:
        """Park the automation, servicing operator commands, until handback.

        This is the resume point. When it returns, the lease is back with the
        agent under a fresh token and the surface is wherever the human left it.
        """
        request = self._get(intervention_id)
        deadline = time.monotonic() + timeout_s
        self.evidence.event("awaiting_operator", intervention=intervention_id,
                            timeout_s=timeout_s)
        while time.monotonic() < deadline:
            if request.status in (InterventionStatus.RETURNED, InterventionStatus.RESOLVED):
                return request
            self.pump(timeout=0.2)
        request.status = InterventionStatus.ABANDONED
        self.evidence.event("intervention_abandoned", intervention=intervention_id)
        return request

    # -- operator actions (executed on the surface-owning thread) -----------

    def operator_observe(self, token: str) -> Observation:
        self.assert_control(ControlOwner.OPERATOR, token)
        return self.surface.perceive(0)

    def operator_act(
        self, intervention_id: str, token: str, *, kind: ActionKind,
        handle: str | None = None, value: str | None = None, key: str | None = None,
    ) -> ActResult:
        """One human action on the live session, recorded as evidence.

        Recording these is not bookkeeping. It is what makes a handoff auditable
        -- and it is the raw material for noticing that the same manual fix keeps
        being needed and belongs in the artifact.
        """
        self.assert_control(ControlOwner.OPERATOR, token)
        request = self._get(intervention_id)
        observation = self.surface.perceive(0)
        node = observation.node(handle) if handle else None
        if kind is not ActionKind.PRESS and node is None and kind is not ActionKind.NAVIGATE:
            return ActResult(False, error=f"no control {handle!r} on screen")
        result = self.surface.act(ActRequest(kind=kind, node=node, value=value,
                                             url=value if kind is ActionKind.NAVIGATE else None,
                                             key=key))
        self.surface.settle()
        request.operator_actions.append(OperatorAction(
            kind=kind.value,
            target=(node.label if node else (key or value or "")),
            # Scrubbed by the evidence writer on the way to disk.
            value=value,
            note=result.detail or result.error or "",
        ))
        self.evidence.event("operator_action", intervention=intervention_id,
                            action=kind.value, target=(node.label if node else key),
                            value=value, ok=result.ok, detail=result.detail or result.error)
        return result


class LeasedSurface:
    """A Surface that refuses writes from whoever does not hold the lease.

    Reads stay open to both parties: an operator watching a screenshot while
    automation works, or automation observing after handback, are both fine and
    neither can damage anything. Only acting is gated.
    """

    def __init__(self, inner: Surface, broker: SessionBroker,
                 owner: ControlOwner = ControlOwner.AGENT) -> None:
        self._inner = inner
        self._broker = broker
        self._owner = owner
        self.kind = inner.kind

    def act(self, request: ActRequest) -> ActResult:
        self._broker.assert_control(self._owner)
        return self._inner.act(request)

    def navigate(self, url: str) -> ActResult:
        self._broker.assert_control(self._owner)
        return self._inner.navigate(url)

    def __getattr__(self, item: str) -> Any:
        # perceive / settle / screenshot / snapshot / page_text / current_url
        return getattr(self._inner, item)
