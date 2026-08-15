"""Phase 14: the remote-control audit log.

Every remote action leaves a record, so Bhuvan can answer "what did this laptop
do while I was away" without trusting anyone's memory. The log is the one place
where the temptation to record "just a bit more context" is strongest, so the
rules are enforced by code rather than by discipline:

  * the log stores *what was done*, never *what was said* — no message bodies,
    no model replies, no email text, no document contents, no pixels
  * a fixed set of fields is written; anything else a caller passes is dropped
    rather than serialized, so a future capability cannot widen the record by
    accident
  * values are scrubbed for secret-shaped strings before they are written
  * unaddressed personal notes are never logged at all; they only increment a
    counter, which lives elsewhere (`security.ignored_counter`)

The file is JSON Lines under `runtime/`, which is gitignored, and it rotates by
size so it cannot fill the disk.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
from pathlib import Path

log = logging.getLogger("nexus.audit")

_RUNTIME = Path(__file__).resolve().parents[1] / "runtime"
AUDIT_PATH = _RUNTIME / "remote_audit.jsonl"

#: Rotate at 2 MB and keep one previous file. Enough history to review a few
#: weeks of remote use, bounded enough that it never becomes a disk problem.
MAX_BYTES = 2 * 1024 * 1024

#: The complete vocabulary of the record. A caller cannot add fields.
FIELDS = ("time", "action", "target", "outcome", "detail", "confirmed",
          "duration_ms")

#: Outcomes are a closed set so the log stays queryable.
OUTCOMES = ("ok", "refused", "failed", "expired", "confirmed", "cancelled")

_MAX_DETAIL_CHARS = 200

#: Anything shaped like a credential is replaced rather than written. These
#: catch the realistic accidents: a token echoed into an error string, a path
#: containing a key file, an authorization header repeated back by a library.
_SECRET_PATTERNS = (
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                       # hex tokens
    re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}\."   # JWTs
               r"[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:bearer|token|apikey|api_key|password|passwd|secret|"
               r"authorization)\b\s*[:=]?\s*\S+"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{10,}\b"),                 # Google keys
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),                  # OpenAI-style
)

_REDACTED = "[redacted]"

_lock = threading.Lock()


def scrub(text: str) -> str:
    """Remove secret-shaped substrings and clamp the length.

    This is a backstop, not a licence to pass sensitive text: callers must
    write a description of the action, not the content it acted on.
    """
    value = str(text or "")
    for pattern in _SECRET_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    if len(value) > _MAX_DETAIL_CHARS:
        value = value[:_MAX_DETAIL_CHARS] + "…"
    return value


def _rotate_if_needed() -> None:
    try:
        if AUDIT_PATH.exists() and AUDIT_PATH.stat().st_size >= MAX_BYTES:
            previous = AUDIT_PATH.with_suffix(".jsonl.1")
            os.replace(AUDIT_PATH, previous)
    except OSError as exc:
        log.debug("audit rotation failed: %s", exc)


def record(action: str, outcome: str, *, target: str = "", detail: str = "",
           confirmed: bool = False, duration_ms: int | None = None) -> dict:
    """Append one record and return it.

    Never raises: an audit failure must not cancel the action that was already
    performed, and must not become a way to crash the bridge.
    """
    entry = {
        "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "action": str(action)[:64],
        "target": str(target)[:32],
        "outcome": outcome if outcome in OUTCOMES else "failed",
        "detail": scrub(detail),
        "confirmed": bool(confirmed),
        "duration_ms": int(duration_ms) if duration_ms is not None else None,
    }
    try:
        with _lock:
            _RUNTIME.mkdir(parents=True, exist_ok=True)
            _rotate_if_needed()
            with AUDIT_PATH.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("audit write failed: %s", exc)
    return entry


def recent(limit: int = 20) -> list[dict]:
    """The most recent records, newest last. Empty when nothing is logged."""
    try:
        with _lock:
            if not AUDIT_PATH.exists():
                return []
            lines = AUDIT_PATH.read_text(encoding="utf-8",
                                         errors="replace").splitlines()
    except OSError as exc:
        log.debug("audit read failed: %s", exc)
        return []

    entries = []
    for line in lines[-max(1, limit):]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return entries


def format_recent(limit: int = 10) -> str:
    """A phone-sized summary of what this laptop has been asked to do."""
    entries = recent(limit)
    if not entries:
        return "No remote actions have been recorded yet."

    lines = [f"Last {len(entries)} remote action"
             f"{'s' if len(entries) != 1 else ''}:"]
    for entry in entries:
        stamp = str(entry.get("time", ""))[11:16]
        # ASCII marks: this string is printed to a cp1252 Windows console as
        # well as sent to a phone, and a UnicodeEncodeError must never be the
        # thing that hides the audit log.
        mark = "ok" if entry.get("outcome") in ("ok", "confirmed") else "--"
        detail = entry.get("detail") or ""
        lines.append(f"{stamp} {mark} {entry.get('action', '?')}"
                     + (f" — {detail}" if detail else ""))
    return "\n".join(lines)
