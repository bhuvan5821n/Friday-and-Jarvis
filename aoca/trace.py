"""Trace context: one id per user request, carried across threads.

`contextvars`, not a module-level global. The assistant runs the Qt main thread,
a worker per agent task, and an asyncio loop for the browser tools; a plain
global would interleave three requests into one trace and make every later
measurement meaningless.

`contextvars` does not propagate into `ThreadPoolExecutor` or `Thread` by
itself, so the handoff is explicit: `bind()` wraps a callable together with a
snapshot of the current context. If a stage forgets to bind, its events land
with no trace id — visibly orphaned, rather than silently attached to whichever
request happened to run last on that thread.
"""
from __future__ import annotations

import contextvars
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import Any, TypeVar


class Assistant(str, Enum):
    """Which identity is acting. `assistant_scope` in the AOCA spec."""

    SHARED = "SHARED"
    JARVIS = "JARVIS"
    FRIDAY = "FRIDAY"
    NEXUS = "NEXUS"


def new_trace_id() -> str:
    """Short, sortable, unique. Time prefix so a log reads chronologically."""
    return f"{int(time.time()):x}-{uuid.uuid4().hex[:8]}"


@dataclass(frozen=True)
class TraceContext:
    """Everything a stage needs to know about the request it is inside.

    Immutable — a child stage gets a `replace()`d copy rather than mutating the
    parent, so a nested tool cannot silently extend its caller's deadline.
    """

    trace_id: str
    assistant: Assistant = Assistant.SHARED
    origin: str = "unknown"
    stage: str = "root"
    parent_span: str | None = None
    span_id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    deadline: float | None = None
    cancel: threading.Event | None = None

    @property
    def cancelled(self) -> bool:
        return bool(self.cancel and self.cancel.is_set())

    @property
    def expired(self) -> bool:
        return self.deadline is not None and time.monotonic() > self.deadline

    def remaining(self) -> float | None:
        """Seconds left, floored at zero. None when no deadline was set."""
        if self.deadline is None:
            return None
        return max(0.0, self.deadline - time.monotonic())

    def child(self, stage: str) -> TraceContext:
        return replace(self, stage=stage, parent_span=self.span_id,
                       span_id=uuid.uuid4().hex[:8])

    def as_fields(self) -> dict[str, Any]:
        """Safe to log. Contains no request text by construction."""
        return {
            "trace_id": self.trace_id,
            "assistant": self.assistant.value,
            "origin": self.origin,
            "stage": self.stage,
            "span_id": self.span_id,
            "parent_span": self.parent_span,
        }


_ROOT = TraceContext(trace_id="untraced", stage="none")
_current: contextvars.ContextVar[TraceContext] = contextvars.ContextVar(
    "aoca_trace", default=_ROOT)


def current() -> TraceContext:
    return _current.get()


def is_traced() -> bool:
    return _current.get() is not _ROOT


@contextmanager
def trace(origin: str = "unknown",
          assistant: Assistant | str = Assistant.SHARED,
          timeout: float | None = None,
          cancel: threading.Event | None = None,
          trace_id: str | None = None):
    """Open a trace for one user request. Wrap the whole request, not a stage."""
    if not isinstance(assistant, Assistant):
        try:
            assistant = Assistant(str(assistant).strip().upper())
        except ValueError:
            assistant = Assistant.SHARED
    context = TraceContext(
        trace_id=trace_id or new_trace_id(),
        assistant=assistant,
        origin=str(origin),
        stage="request",
        deadline=None if timeout is None else time.monotonic() + timeout,
        cancel=cancel,
    )
    token = _current.set(context)
    try:
        yield context
    finally:
        _current.reset(token)


@contextmanager
def stage(name: str):
    """Nest a named stage inside the current trace."""
    token = _current.set(_current.get().child(name))
    try:
        yield _current.get()
    finally:
        _current.reset(token)


T = TypeVar("T")


def bind(func: Callable[..., T]) -> Callable[..., T]:
    """Capture the current context so `func` sees it on another thread.

    Required for anything handed to a `Thread`, `ThreadPoolExecutor`, or a Qt
    signal that crosses threads — none of those copy context variables.
    """
    context = contextvars.copy_context()

    def wrapper(*args: Any, **kwargs: Any) -> T:
        return context.run(func, *args, **kwargs)

    wrapper.__name__ = getattr(func, "__name__", "bound")
    return wrapper


def restore(context: TraceContext) -> Callable[[], None]:
    """Adopt a context explicitly and return an undo callable.

    For places where a decorator does not fit — a Qt slot, or a callback whose
    signature is fixed by a library. `bind` is preferred where possible.
    """
    token = _current.set(context)
    return lambda: _current.reset(token)
