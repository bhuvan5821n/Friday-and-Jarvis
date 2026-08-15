"""Thread-safe event bus used by JARVIS core, studios, and future plugins.

Handlers are isolated: one unhealthy plugin must never prevent the remaining
subscribers from receiving an event.  Events are deliberately small, typed
data objects so transports can be replaced later without changing studios.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import threading
from typing import Any
from uuid import uuid4


@dataclass(frozen=True, slots=True)
class Event:
    topic: str
    payload: dict[str, Any] = field(default_factory=dict)
    source: str = "core"
    correlation_id: str = field(default_factory=lambda: uuid4().hex)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


EventHandler = Callable[[Event], None]


class EventBus:
    """In-process publish/subscribe bus with exact-topic and wildcard support."""

    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)
        self._lock = threading.RLock()

    def subscribe(self, topic: str, handler: EventHandler) -> Callable[[], None]:
        if not topic:
            raise ValueError("event topic is required")
        with self._lock:
            if handler not in self._handlers[topic]:
                self._handlers[topic].append(handler)

        def unsubscribe() -> None:
            with self._lock:
                try:
                    self._handlers[topic].remove(handler)
                except (KeyError, ValueError):
                    pass
        return unsubscribe

    def publish(self, event: Event) -> list[Exception]:
        """Publish synchronously and return handler failures for observability."""
        with self._lock:
            handlers = tuple(self._handlers.get(event.topic, ())) + tuple(
                self._handlers.get("*", ()))
        failures: list[Exception] = []
        for handler in handlers:
            try:
                handler(event)
            except Exception as exc:  # Plugin failures must remain contained.
                failures.append(exc)
        return failures

    def clear(self) -> None:
        with self._lock:
            self._handlers.clear()


bus = EventBus()
