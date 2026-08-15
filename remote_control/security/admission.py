"""The single admission decision, and the only record kept of what was ignored.

Every inbound WhatsApp message passes through `admit()` exactly once, before
anything else happens to it. The counter file is the *entire* audit trail for
ignored messages: a number, never a body, never a sender, never a timestamp
precise enough to correlate with a specific note.
"""
from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from .prefix_gate import GateDecision, parse_prefix
from .sender_gate import AuthorizedSender, load_authorized

#: Counter lives beside other runtime state, not in the repo.
DEFAULT_COUNTER = Path(__file__).resolve().parents[2] / "runtime" / "remote_ignored.json"

_lock = threading.Lock()


@dataclass(frozen=True)
class Admission:
    """Result of the full gate. `decision.command` is the only field holding
    user text; the rest is safe to log."""

    allowed: bool
    decision: GateDecision
    reason: str

    @property
    def target(self) -> str | None:
        return self.decision.target if self.allowed else None

    @property
    def command(self) -> str:
        return self.decision.command if self.allowed else ""


class AdmissionController:
    """Holds the allowlist and the ignored counter. Create one per gateway."""

    def __init__(self, authorized: AuthorizedSender | None = None,
                 counter_path: Path | str | None = None):
        self._authorized = authorized if authorized is not None else load_authorized()
        self._counter_path = Path(counter_path) if counter_path else DEFAULT_COUNTER

    @property
    def configured(self) -> bool:
        """False when no allowlist is present — the gateway must not run."""
        return self._authorized.configured

    def admit(self, sender: object, message: object) -> Admission:
        """Sender first, then prefix. Both must pass.

        An unauthorized sender is not counted: counting it would let a stranger
        move a number on this laptop. Only Bhuvan's own unprefixed notes are
        counted, and only as a total.
        """
        if not self._authorized.allows(sender):
            return Admission(False, GateDecision(False, reason="unauthorized_sender"),
                             "unauthorized_sender")

        decision = parse_prefix(message)
        if not decision.addressed:
            self.record_ignored()
            return Admission(False, decision, decision.reason)

        return Admission(True, decision, "addressed")

    # ---- ignored-message counter (a number, nothing else) ----------------

    def record_ignored(self) -> int:
        with _lock:
            count = self._read() + 1
            self._write(count)
            return count

    def ignored_count(self) -> int:
        with _lock:
            return self._read()

    def _read(self) -> int:
        try:
            data = json.loads(self._counter_path.read_text(encoding="utf-8"))
            value = data.get("ignored_unprefixed_message_count", 0)
            return value if isinstance(value, int) and value >= 0 else 0
        except Exception:
            return 0

    def _write(self, count: int) -> None:
        try:
            self._counter_path.parent.mkdir(parents=True, exist_ok=True)
            # Atomic replace: a crash mid-write must not corrupt the counter
            # into a state that reads as 0 and hides activity.
            fd, tmp = tempfile.mkstemp(dir=str(self._counter_path.parent),
                                       suffix=".tmp")
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump({"ignored_unprefixed_message_count": count}, fh)
            os.replace(tmp, self._counter_path)
        except Exception:
            pass   # losing a counter increment must never break the gate
