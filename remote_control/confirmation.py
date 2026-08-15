"""Phase 9: the confirmation state machine for dangerous remote actions.

Nothing that changes the machine happens on the strength of one WhatsApp
message. A dangerous request becomes a *pending* action with a short-lived
token; it runs only when Bhuvan replies CONFIRM with that token, and it is
discarded on CANCEL, on expiry, or as soon as any newer request arrives.

The rules this enforces, each one a test:

  * a pending action expires after two minutes — an old CONFIRM is not a
    standing authorization
  * CONFIRM is bound to one specific action, not to "whatever was last asked",
    so a delayed confirmation cannot land on a different action than the one
    Bhuvan read
  * a token is single-use: replaying a CONFIRM does not run the action twice
  * only one action is ever pending, so CONFIRM is never ambiguous
  * the stored action never contains the raw WhatsApp message

This module decides *whether* something may run. It never runs anything: the
executor is passed in by the caller, which keeps this file free of any
capability of its own.
"""
from __future__ import annotations

import re
import secrets
import threading
import time
from dataclasses import dataclass, field

#: A confirmation older than this is refused. Long enough to read a phone
#: notification and reply, short enough that a forgotten prompt cannot be
#: triggered by an unrelated later message.
EXPIRY_SECONDS = 120.0

_CONFIRM_RE = re.compile(r"^\s*(?:/)?(confirm|yes|do it|go ahead|proceed)\b"
                         r"[\s,:.!-]*([a-z0-9]{4,12})?\s*$", re.IGNORECASE)
_CANCEL_RE = re.compile(r"^\s*(?:/)?(cancel|no|stop|abort|nevermind|never mind)\b"
                        r"[\s,:.!-]*([a-z0-9]{4,12})?\s*$", re.IGNORECASE)


class ConfirmationError(RuntimeError):
    """The action could not be confirmed. The message is safe to send back."""


@dataclass(frozen=True)
class PendingAction:
    """One dangerous action awaiting confirmation.

    `summary` is written by the caller for Bhuvan to read; it must describe the
    action, not echo the message that requested it.
    """

    token: str
    action: str
    summary: str
    params: dict = field(default_factory=dict)
    created: float = 0.0

    def expired(self, now: float | None = None) -> bool:
        return (time.time() if now is None else now) - self.created > EXPIRY_SECONDS

    def __repr__(self) -> str:
        return (f"PendingAction(action={self.action!r}, token={self.token!r}, "
                f"params=<{len(self.params)} keys redacted>)")

    __str__ = __repr__


def parse_confirmation(message: str) -> tuple[str | None, str | None]:
    """(verb, token) where verb is 'confirm', 'cancel', or None.

    The token is optional in the message: a bare CONFIRM is accepted because
    only one action is ever pending. When a token *is* given it must match, so
    a stale prompt read later cannot confirm the wrong thing.
    """
    text = str(message or "")
    match = _CONFIRM_RE.match(text)
    if match:
        return "confirm", (match.group(2) or None)
    match = _CANCEL_RE.match(text)
    if match:
        return "cancel", (match.group(2) or None)
    return None, None


class ConfirmationManager:
    """Holds at most one pending action for the single authorized user."""

    def __init__(self, expiry: float = EXPIRY_SECONDS):
        self._expiry = expiry
        self._lock = threading.Lock()
        self._pending: PendingAction | None = None
        self._used_tokens: set[str] = set()

    # ---- requesting ------------------------------------------------------

    def request(self, action: str, summary: str, params: dict | None = None
                ) -> PendingAction:
        """Park a dangerous action and return the prompt to send to Bhuvan.

        Replaces any earlier pending action: the most recent request is the one
        Bhuvan is looking at, and leaving a queue would make CONFIRM ambiguous.
        """
        token = secrets.token_hex(3)
        pending = PendingAction(token=token, action=action, summary=summary,
                                params=dict(params or {}), created=time.time())
        with self._lock:
            self._pending = pending
        return pending

    def prompt_text(self, pending: PendingAction) -> str:
        minutes = int(self._expiry // 60)
        return (f"{pending.summary}\n\n"
                f"Reply CONFIRM {pending.token} to go ahead, or CANCEL. "
                f"This expires in {minutes} minute"
                f"{'s' if minutes != 1 else ''}.")

    @property
    def pending(self) -> PendingAction | None:
        with self._lock:
            if self._pending and self._pending.expired():
                self._pending = None
            return self._pending

    # ---- resolving -------------------------------------------------------

    def confirm(self, token: str | None = None) -> PendingAction:
        """Claim the pending action. Raises ConfirmationError if it cannot run.

        The token is consumed here, before the caller executes anything, so a
        replayed CONFIRM cannot run the action a second time.
        """
        with self._lock:
            pending = self._pending
            if pending is None:
                raise ConfirmationError(
                    "There is nothing waiting for confirmation.")
            if pending.expired():
                self._pending = None
                raise ConfirmationError(
                    "That request expired. Send it again if you still want it.")
            if token and token.lower() != pending.token.lower():
                # Do not clear the pending action: a mistyped token should not
                # cost Bhuvan the confirmation he is in the middle of giving.
                raise ConfirmationError(
                    f"That confirmation code does not match the request "
                    f"waiting. Reply CONFIRM {pending.token} to go ahead.")
            self._pending = None
            self._used_tokens.add(pending.token)
            if len(self._used_tokens) > 500:
                self._used_tokens = {pending.token}
            return pending

    def cancel(self) -> PendingAction | None:
        with self._lock:
            pending, self._pending = self._pending, None
        return pending

    def clear(self) -> None:
        with self._lock:
            self._pending = None

    def was_used(self, token: str) -> bool:
        with self._lock:
            return token in self._used_tokens
