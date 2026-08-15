"""Typed cognitive events on a non-blocking bus.

Two decisions shape this file.

**It never blocks the caller.** Publishing happens on a dispatcher thread with a
bounded queue, because the highest-value publish sites are on the Qt main
thread. A slow subscriber must degrade telemetry, not freeze the window.

**Under overload it drops telemetry and keeps consequences.** When the queue is
full, low-value events (stage timings, progress) are discarded; security
decisions, denials and failures are not — those are the events someone will
later need to explain what happened. A bus that drops uniformly loses exactly
the records that matter, since failures cluster with load.

Delivery is at-most-once and unordered across priorities. That is acceptable
here: nothing in Phases 1-3 makes a decision from an event. Events observe.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aoca import trace
from aoca.config import flags, limits
from aoca.privacy import sanitize

log = logging.getLogger("aoca.events")


class EventState(str, Enum):
    """The lifecycle of one request. Terminal states are terminal."""

    RECEIVED = "received"
    ROUTED = "routed"
    POLICY_CHECKED = "policy_checked"
    EXECUTING = "executing"
    EXECUTED = "executed"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    FAILED = "failed"
    REFUSED = "refused"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNVERIFIED = "unverified"


TERMINAL_STATES = frozenset({
    EventState.COMPLETED, EventState.FAILED, EventState.REFUSED,
    EventState.CANCELLED, EventState.TIMED_OUT, EventState.UNVERIFIED,
})

#: Legal transitions. Enforced by `StateMachine`, not by the publish path — an
#: illegal transition is a bug to surface, never a reason to lose the event.
_TRANSITIONS: dict[EventState, frozenset[EventState]] = {
    EventState.RECEIVED: frozenset({
        EventState.ROUTED, EventState.REFUSED, EventState.CANCELLED,
        EventState.FAILED, EventState.TIMED_OUT}),
    EventState.ROUTED: frozenset({
        EventState.POLICY_CHECKED, EventState.REFUSED, EventState.CANCELLED,
        EventState.FAILED, EventState.TIMED_OUT}),
    EventState.POLICY_CHECKED: frozenset({
        EventState.EXECUTING, EventState.REFUSED, EventState.CANCELLED,
        EventState.FAILED, EventState.TIMED_OUT}),
    EventState.EXECUTING: frozenset({
        EventState.EXECUTED, EventState.FAILED, EventState.CANCELLED,
        EventState.TIMED_OUT}),
    EventState.EXECUTED: frozenset({
        EventState.VERIFYING, EventState.UNVERIFIED, EventState.COMPLETED,
        EventState.FAILED, EventState.TIMED_OUT}),
    EventState.VERIFYING: frozenset({
        EventState.COMPLETED, EventState.FAILED, EventState.UNVERIFIED,
        EventState.TIMED_OUT, EventState.CANCELLED}),
}


class Priority(int, Enum):
    """Drop order under overload. CONSEQUENCE is never dropped."""

    CONSEQUENCE = 0   # denials, failures, security decisions, outcomes
    LIFECYCLE = 1     # state transitions
    TELEMETRY = 2     # timings, progress, queue depth


#: Event types that record a consequence. Matched by prefix.
_CONSEQUENCE_PREFIXES = ("policy.", "security.", "action.outcome",
                         "action.failed", "action.refused", "error.")


def _priority_for(event_type: str, state: EventState | None) -> Priority:
    if any(event_type.startswith(p) for p in _CONSEQUENCE_PREFIXES):
        return Priority.CONSEQUENCE
    if state in (EventState.FAILED, EventState.REFUSED, EventState.TIMED_OUT,
                 EventState.UNVERIFIED):
        return Priority.CONSEQUENCE
    if state is not None:
        return Priority.LIFECYCLE
    return Priority.TELEMETRY


@dataclass(frozen=True)
class CognitiveEvent:
    """One observation. Payload is sanitized at construction, not at write.

    Sanitizing here means an event object cannot hold content even in memory,
    so a subscriber that logs the whole event cannot leak what the filter
    already refused.
    """

    event_type: str
    state: EventState | None = None
    trace_id: str = "untraced"
    span_id: str = ""
    parent_span: str | None = None
    assistant: str = "SHARED"
    origin: str = "unknown"
    stage: str = "none"
    payload: dict[str, Any] = field(default_factory=dict)
    occurred_at: float = field(default_factory=time.time)
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    priority: Priority = Priority.TELEMETRY

    @classmethod
    def create(cls, event_type: str, state: EventState | None = None,
               payload: dict[str, Any] | None = None,
               context: trace.TraceContext | None = None) -> CognitiveEvent:
        context = context or trace.current()
        return cls(
            event_type=str(event_type)[:64],
            state=state,
            trace_id=context.trace_id,
            span_id=context.span_id,
            parent_span=context.parent_span,
            assistant=context.assistant.value,
            origin=context.origin,
            stage=context.stage,
            payload=sanitize(payload),
            priority=_priority_for(str(event_type), state),
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "state": self.state.value if self.state else None,
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span": self.parent_span,
            "assistant": self.assistant,
            "origin": self.origin,
            "stage": self.stage,
            "occurred_at": self.occurred_at,
            "priority": self.priority.value,
            **self.payload,
        }


class StateMachine:
    """Per-trace state, so an illegal transition is caught rather than logged.

    Bounded: at most `_MAX_TRACES` are tracked, oldest evicted. A long session
    must not accumulate one entry per request forever.
    """

    _MAX_TRACES = 256

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._states: dict[str, EventState] = {}

    def advance(self, trace_id: str, state: EventState) -> bool:
        """True if the transition was legal. False does not lose the event."""
        with self._lock:
            previous = self._states.get(trace_id)
            if previous is None:
                legal = state is EventState.RECEIVED
            elif previous in TERMINAL_STATES:
                legal = False
            else:
                legal = state in _TRANSITIONS.get(previous, frozenset())

            if legal:
                self._states[trace_id] = state
                if len(self._states) > self._MAX_TRACES:
                    oldest = next(iter(self._states))
                    self._states.pop(oldest, None)
            return legal

    def state_of(self, trace_id: str) -> EventState | None:
        with self._lock:
            return self._states.get(trace_id)

    def forget(self, trace_id: str) -> None:
        with self._lock:
            self._states.pop(trace_id, None)


@dataclass
class BusStats:
    published: int = 0
    delivered: int = 0
    dropped: int = 0
    handler_errors: int = 0
    illegal_transitions: int = 0
    max_queue_depth: int = 0


class CognitiveBus:
    """Bounded queue, one dispatcher thread, at-most-once delivery."""

    def __init__(self, maxsize: int | None = None) -> None:
        self._queue: queue.Queue[CognitiveEvent | None] = queue.Queue(
            maxsize=maxsize or limits.EVENT_QUEUE_MAX)
        self._lock = threading.RLock()
        self._handlers: dict[str, list[Callable[[CognitiveEvent], None]]] = {}
        self._thread: threading.Thread | None = None
        self._stopping = threading.Event()
        self.stats = BusStats()
        self.states = StateMachine()

    # ---- subscription ----------------------------------------------------

    def subscribe(self, event_type: str,
                  handler: Callable[[CognitiveEvent], None]
                  ) -> Callable[[], None]:
        """Subscribe to one type, or `"*"` for all. Returns an unsubscribe."""
        with self._lock:
            handlers = self._handlers.setdefault(event_type, [])
            if handler not in handlers:
                handlers.append(handler)

        def unsubscribe() -> None:
            with self._lock:
                if handler in self._handlers.get(event_type, []):
                    self._handlers[event_type].remove(handler)

        return unsubscribe

    # ---- publishing ------------------------------------------------------

    def publish(self, event_type: str, state: EventState | None = None,
                payload: dict[str, Any] | None = None,
                context: trace.TraceContext | None = None) -> bool:
        """Enqueue an event. Returns False if it was dropped.

        Never blocks and never raises. Callers do not check the result — a
        publish site that has to handle a telemetry failure is a publish site
        that will be deleted.
        """
        if not flags.enabled("AOCA_EVENTS_ENABLED"):
            return False
        try:
            event = CognitiveEvent.create(event_type, state, payload, context)
        except Exception as exc:
            log.debug("event construction failed: %s", exc)
            return False

        if state is not None and not self.states.advance(event.trace_id, state):
            with self._lock:
                self.stats.illegal_transitions += 1

        return self._enqueue(event)

    def _enqueue(self, event: CognitiveEvent) -> bool:
        self._ensure_running()
        try:
            self._queue.put_nowait(event)
        except queue.Full:
            if event.priority is not Priority.CONSEQUENCE:
                with self._lock:
                    self.stats.dropped += 1
                return False
            # A consequence must survive. Evict the lowest-value pending item
            # rather than the record of what actually happened.
            if not self._evict_one():
                with self._lock:
                    self.stats.dropped += 1
                return False
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                with self._lock:
                    self.stats.dropped += 1
                return False

        with self._lock:
            self.stats.published += 1
            self.stats.max_queue_depth = max(self.stats.max_queue_depth,
                                             self._queue.qsize())
        return True

    def _evict_one(self) -> bool:
        """Drain one non-consequence event, putting consequences back.

        ponytail: O(queue) worst case, and only on the overload path. A real
        priority queue is the upgrade if overload stops being exceptional.
        """
        held: list[CognitiveEvent] = []
        evicted = False
        try:
            while True:
                item = self._queue.get_nowait()
                if item is None:
                    held.append(item)  # type: ignore[arg-type]
                    continue
                if not evicted and item.priority is not Priority.CONSEQUENCE:
                    evicted = True
                    with self._lock:
                        self.stats.dropped += 1
                    continue
                held.append(item)
        except queue.Empty:
            pass
        for item in held:
            try:
                self._queue.put_nowait(item)
            except queue.Full:
                with self._lock:
                    self.stats.dropped += 1
        return evicted

    # ---- dispatch --------------------------------------------------------

    def _ensure_running(self) -> None:
        with self._lock:
            if self._stopping.is_set():
                return
            if self._thread is not None and self._thread.is_alive():
                return
            self._thread = threading.Thread(
                target=self._run, name="aoca-events", daemon=True)
            self._thread.start()

    def _run(self) -> None:
        while True:
            try:
                event = self._queue.get(timeout=0.5)
            except queue.Empty:
                if self._stopping.is_set():
                    return
                continue
            if event is None:
                return
            self._deliver(event)

    def _deliver(self, event: CognitiveEvent) -> None:
        with self._lock:
            handlers = list(self._handlers.get(event.event_type, ()))
            handlers += list(self._handlers.get("*", ()))
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:
                # One bad subscriber must not stop the others or kill the
                # dispatcher thread, which would silently end all telemetry.
                with self._lock:
                    self.stats.handler_errors += 1
                log.debug("event handler failed for %s: %s",
                          event.event_type, exc)
        with self._lock:
            self.stats.delivered += 1

    # ---- lifecycle -------------------------------------------------------

    def drain(self, timeout: float = 2.0) -> bool:
        """Wait for the queue to empty. For tests and for shutdown."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._queue.empty():
                time.sleep(0.02)   # let the in-flight event finish delivering
                return self._queue.empty()
            time.sleep(0.01)
        return False

    def shutdown(self, timeout: float = 2.0) -> None:
        """Stop accepting, deliver what is queued, join the thread."""
        self._stopping.set()
        self.drain(timeout)
        with self._lock:
            thread = self._thread
            self._thread = None
        if thread is not None and thread.is_alive():
            try:
                self._queue.put_nowait(None)
            except queue.Full:
                pass
            thread.join(timeout=timeout)

    def reset(self) -> None:
        """For tests. Clears handlers, stats and pending events."""
        self.shutdown(timeout=1.0)
        with self._lock:
            self._handlers.clear()
            self.stats = BusStats()
            self.states = StateMachine()
        while not self._queue.empty():
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break
        self._stopping.clear()


bus = CognitiveBus()


def emit(event_type: str, state: EventState | None = None, **payload: Any) -> bool:
    """Shorthand for the publish sites. Keyword payload, never positional."""
    return bus.publish(event_type, state, payload)
