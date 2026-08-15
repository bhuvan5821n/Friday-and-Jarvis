"""NEXUS bridge wire protocol — typed, authenticated, replay-protected.

The Hermes gateway and the assistant are separate processes with incompatible
dependency trees, so they talk over a loopback socket rather than sharing
imports. This module is the single definition of what may cross that boundary,
imported by both sides so they cannot drift.

Design constraints, all of which the tests enforce:

  * 127.0.0.1 only — never 0.0.0.0, never a routable interface
  * every request carries the Credential Manager token, compared in constant time
  * every request carries a timestamp and a nonce; stale or repeated requests
    are refused, so a captured frame cannot be replayed
  * only the exact action names in ACTIONS are accepted
  * bounded frame size and a hard timeout, so a stuck or hostile client cannot
    exhaust the assistant
  * no action carries a shell string; parameters are a small validated object
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass, field
from typing import Any

#: Loopback only. Named as a constant so a review of "what do we bind to" has
#: exactly one answer.
BIND_HOST = "127.0.0.1"

#: Distinct from the lifecycle (48757) and SpeechCore (48800) channels.
BRIDGE_PORT = 48810

#: Requests older than this are refused. Long enough to survive a slow poll
#: cycle, short enough that a captured frame is useless by the time it leaks.
MAX_CLOCK_SKEW_SECONDS = 120.0

#: A frame larger than this is refused before parsing.
MAX_FRAME_BYTES = 64 * 1024

#: Socket read/write timeout.
IO_TIMEOUT_SECONDS = 10.0

PROTOCOL_VERSION = 1

#: Every action the assistant will honour over the bridge. Anything else is
#: refused by both encode() and decode(), so neither side can be talked into
#: an operation this file does not list.
#:
#: Phase 2 ships the transport with a read-only vocabulary. Dangerous actions
#: are deliberately absent until their own phases pass their tests.
ACTIONS = frozenset({
    "PING",            # liveness; carries no data
    "STATUS",          # read-only system status
    "WHATS_HAPPENING", # what the assistant is doing right now
    "ASK",             # route a natural-language command to an assistant
    "AUDIT",           # read back what this laptop was asked to do
})

#: Which assistant a request is addressed to. "nexus" means the neutral
#: gateway itself — status and transport concerns, no personality.
TARGETS = frozenset({"jarvis", "friday", "nexus"})


class ProtocolError(ValueError):
    """A frame was malformed, unauthorized, stale, or replayed."""


@dataclass(frozen=True)
class Request:
    action: str
    target: str = "nexus"
    command: str = ""
    params: dict = field(default_factory=dict)
    request_id: str = ""

    def __repr__(self) -> str:            # pragma: no cover - formatting only
        # `command` is user text from WhatsApp; keep it out of logs by default.
        return (f"Request(action={self.action!r}, target={self.target!r}, "
                f"command=<{len(self.command)} chars>, id={self.request_id!r})")

    __str__ = __repr__


@dataclass(frozen=True)
class Response:
    ok: bool
    text: str = ""
    data: dict = field(default_factory=dict)
    error: str = ""
    request_id: str = ""


def _canonical(body: dict) -> bytes:
    """Stable serialization for signing. Sorted keys so both sides agree."""
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")


def sign(body: dict, token: str) -> str:
    """HMAC over the whole frame.

    Signing rather than sending the token means the secret never travels the
    wire, and the signature covers the timestamp and nonce — so an attacker who
    observes a frame cannot alter its contents or its freshness.
    """
    return hmac.new(token.encode("utf-8"), _canonical(body),
                    hashlib.sha256).hexdigest()


def encode_request(request: Request, token: str) -> bytes:
    if request.action not in ACTIONS:
        raise ProtocolError(f"refusing to send unknown action {request.action!r}")
    if request.target not in TARGETS:
        raise ProtocolError(f"refusing to send unknown target {request.target!r}")
    body = {
        "v": PROTOCOL_VERSION,
        "action": request.action,
        "target": request.target,
        "command": request.command,
        "params": request.params or {},
        "request_id": request.request_id or secrets.token_hex(8),
        "ts": time.time(),
        "nonce": secrets.token_hex(16),
    }
    frame = {"body": body, "sig": sign(body, token)}
    raw = (json.dumps(frame) + "\n").encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("request is too large")
    return raw


def decode_request(raw: bytes, token: str, seen: "NonceCache",
                   now: float | None = None) -> Request:
    """Parse, authenticate, and freshness-check one request.

    Raises ProtocolError for every failure mode. The caller must not
    distinguish between them in anything it sends back: a client that can tell
    "bad signature" from "replayed nonce" learns something about the secret.
    """
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("frame too large")
    try:
        frame = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ProtocolError("malformed frame") from exc
    if not isinstance(frame, dict):
        raise ProtocolError("malformed frame")

    body, sig = frame.get("body"), frame.get("sig")
    if not isinstance(body, dict) or not isinstance(sig, str):
        raise ProtocolError("malformed frame")

    # Authenticate before looking at anything else in the body, so unsigned
    # input never reaches validation logic.
    if not hmac.compare_digest(sign(body, token), sig):
        raise ProtocolError("bad signature")

    if body.get("v") != PROTOCOL_VERSION:
        raise ProtocolError("unsupported protocol version")

    ts = body.get("ts")
    if not isinstance(ts, (int, float)):
        raise ProtocolError("missing timestamp")
    now = time.time() if now is None else now
    # Future-dated frames are refused too: without that, a captured frame
    # stamped far ahead would stay valid indefinitely.
    if abs(now - float(ts)) > MAX_CLOCK_SKEW_SECONDS:
        raise ProtocolError("stale timestamp")

    nonce = body.get("nonce")
    if not isinstance(nonce, str) or len(nonce) < 16:
        raise ProtocolError("missing nonce")
    if not seen.add(nonce, float(ts)):
        raise ProtocolError("replayed request")

    action = body.get("action")
    if action not in ACTIONS:
        raise ProtocolError("unknown action")
    target = body.get("target", "nexus")
    if target not in TARGETS:
        raise ProtocolError("unknown target")

    command = body.get("command", "")
    if not isinstance(command, str):
        raise ProtocolError("malformed command")
    params = body.get("params", {})
    if not isinstance(params, dict):
        raise ProtocolError("malformed params")

    return Request(action=action, target=target, command=command,
                   params=params, request_id=str(body.get("request_id", "")))


def encode_response(response: Response, token: str) -> bytes:
    body = {
        "v": PROTOCOL_VERSION,
        "ok": response.ok,
        "text": response.text,
        "data": response.data or {},
        "error": response.error,
        "request_id": response.request_id,
        "ts": time.time(),
    }
    frame = {"body": body, "sig": sign(body, token)}
    raw = (json.dumps(frame) + "\n").encode("utf-8")
    if len(raw) > MAX_FRAME_BYTES:
        # Truncate rather than fail: a huge answer should still deliver
        # something useful and honest about the truncation.
        body["text"] = body["text"][:8000] + "\n[truncated]"
        body["data"] = {}
        frame = {"body": body, "sig": sign(body, token)}
        raw = (json.dumps(frame) + "\n").encode("utf-8")
    return raw


def decode_response(raw: bytes, token: str) -> Response:
    try:
        frame = json.loads(raw.decode("utf-8"))
        body, sig = frame["body"], frame["sig"]
    except Exception as exc:
        raise ProtocolError("malformed response") from exc
    if not hmac.compare_digest(sign(body, token), sig):
        raise ProtocolError("bad response signature")
    return Response(ok=bool(body.get("ok")), text=str(body.get("text", "")),
                    data=body.get("data") or {}, error=str(body.get("error", "")),
                    request_id=str(body.get("request_id", "")))


class NonceCache:
    """Remembers recently-seen nonces so a frame cannot be replayed.

    Bounded by the same window the timestamp check enforces: a nonce older than
    the skew limit can be forgotten, because a frame carrying it would be
    refused as stale anyway. That keeps memory flat on a long-running process.
    """

    def __init__(self, window_seconds: float = MAX_CLOCK_SKEW_SECONDS):
        self._window = window_seconds
        self._seen: dict[str, float] = {}

    def add(self, nonce: str, ts: float) -> bool:
        """True if the nonce is new. False means this is a replay."""
        self._evict(ts)
        if nonce in self._seen:
            return False
        self._seen[nonce] = ts
        return True

    def _evict(self, now: float) -> None:
        cutoff = now - self._window
        if len(self._seen) > 10_000:
            self._seen.clear()      # pathological growth: reset rather than leak
            return
        stale = [n for n, ts in self._seen.items() if ts < cutoff]
        for nonce in stale:
            self._seen.pop(nonce, None)

    def __len__(self) -> int:
        return len(self._seen)
