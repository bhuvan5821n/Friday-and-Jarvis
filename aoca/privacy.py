"""Privacy filter for telemetry.

The primary mechanism is an **allowlist of field names**, not regex. Redaction
is a backstop for text that slipped through; a closed set of permitted keys is
what actually keeps content out. If a caller invents `password` or
`email_body`, the field is dropped because it is not on the list — no pattern
has to recognise it first.

Regex redaction reuses `services.web_intelligence.security.redact_secrets`
(pure `re`, no side effects) plus the two shapes it lacks. There were already
two secret-pattern lists in this repo; a third would drift from both.
"""
from __future__ import annotations

import re
from typing import Any

from aoca.config import limits

try:
    from services.web_intelligence.security import redact_secrets as _base_redact
except Exception:  # pragma: no cover - the service tree may be absent
    def _base_redact(text: str) -> str:
        return text

#: Shapes `web_intelligence` does not cover. Kept minimal on purpose — this is
#: a supplement, not a competing list.
_EXTRA_SECRETS = (
    re.compile(r"\b[A-Fa-f0-9]{32,}\b"),                      # hex tokens
    re.compile(r"\b[A-Za-z0-9_-]{24,}\.[A-Za-z0-9_-]{16,}\."  # JWTs
               r"[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"(?i)\b(?:otp|2fa|mfa|pin|cvv)\b\s*[:=]?\s*\d{3,8}\b"),
    re.compile(r"(?i)-{3,}BEGIN [A-Z ]*PRIVATE KEY-{3,}[\s\S]*?-{3,}END[^-]*-{3,}"),
)

_REDACTED = "[redacted]"

#: Field names permitted in an event payload. Everything else is dropped.
#: Add a name here only after deciding it can appear in a log file, and only if
#: it describes an action rather than its content.
ALLOWED_FIELDS = frozenset({
    # identity and routing
    "trace_id", "span_id", "parent_span", "assistant", "origin", "stage",
    "event_type", "state", "sequence",
    # what was done
    "tool", "action", "verb", "target_kind", "policy_rule", "permitted",
    "risk", "confirmation_required", "verifier", "outcome", "error_code",
    # measurements
    "duration_ms", "latency_ms", "attempt", "count", "size_bytes", "queue_depth",
    "dropped", "exit_code", "pid", "process_create_time", "rows",
    # honest booleans
    "execution_started", "verification_completed", "expected_state_observed",
    "cancelled", "timed_out", "early_exit", "transport_success", "task_success",
    # short, already-scrubbed prose intended for the user
    "safe_reason", "suggestion", "message", "model", "provider",
})

#: Names that must never appear, whatever the caller intends. Redundant with
#: the allowlist, and deliberately so: a future edit that widens the allowlist
#: still cannot let these through.
DENIED_FIELDS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "api_key", "apikey",
    "access_token", "refresh_token", "otp", "pin", "cvv", "card",
    "private_key", "ssh_key", "session", "session_key", "cookie", "cookies",
    "credential", "credentials", "authorization", "auth", "prompt",
    "system_prompt", "chain_of_thought", "reasoning", "thoughts",
    "email_body", "body", "content", "text", "transcript", "raw", "payload",
    "message_text", "user_text", "query_text", "page_content",
})

#: Values shaped like these are dropped even from an allowlisted field, because
#: a caller putting a whole document in `message` is the realistic accident.
_MAX_VALUE_CHARS = limits.SAFE_TEXT_MAX


def redact(text: object) -> str:
    """Scrub and clamp one string."""
    value = _base_redact(str(text if text is not None else ""))
    for pattern in _EXTRA_SECRETS:
        value = pattern.sub(_REDACTED, value)
    if len(value) > _MAX_VALUE_CHARS:
        value = value[:_MAX_VALUE_CHARS] + "…"
    return value


def _clean_value(value: Any) -> Any:
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, (list, tuple)):
        return [_clean_value(v) for v in value[:20]]
    if isinstance(value, dict):
        return sanitize(value)
    return redact(repr(value))


def sanitize(payload: dict[str, Any] | None) -> dict[str, Any]:
    """Drop every field not explicitly permitted, then scrub what remains.

    Returns a new dict. Never raises — a telemetry filter that throws would
    take down the path it is meant to observe.
    """
    if not isinstance(payload, dict):
        return {}
    clean: dict[str, Any] = {}
    dropped = 0
    for key, value in payload.items():
        name = str(key).strip().lower()
        if name in DENIED_FIELDS or name not in ALLOWED_FIELDS:
            dropped += 1
            continue
        try:
            clean[name] = _clean_value(value)
        except Exception:
            dropped += 1
    if dropped:
        # Recorded as a number so an over-eager caller is visible without the
        # filter having to keep what it just refused.
        clean["dropped_fields"] = dropped
    return clean


def is_safe(payload: dict[str, Any] | None) -> bool:
    """True when `payload` would pass through `sanitize` unchanged."""
    return sanitize(payload) == (payload or {})
