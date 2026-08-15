"""The bridge client — used by the Hermes gateway to reach the assistant.

Deliberately tiny and dependency-free (stdlib only), because it is imported
into the Hermes virtualenv. It knows how to open one loopback connection, send
one signed request, and read one signed reply.

"The assistant is not running" is a normal, expected outcome, not an error to
retry into the ground: the laptop may be off, asleep, or the app simply closed.
Callers get an honest `ok=False` with a plain-language reason.
"""
from __future__ import annotations

import socket

from .bridge_protocol import (BIND_HOST, BRIDGE_PORT, IO_TIMEOUT_SECONDS,
                              MAX_FRAME_BYTES, ProtocolError, Request,
                              Response, decode_response, encode_request)
from .security.token_store import TokenUnavailable, read_token

#: What the user sees when the desktop app is not reachable. Deliberately says
#: nothing about why — a "connection refused" is not useful to Bhuvan and the
#: distinction between "off" and "crashed" is not knowable from here.
OFFLINE_MESSAGE = ("The laptop assistant is not reachable right now. It may be "
                   "shut down, asleep, or not running.")


class BridgeClient:
    """One-shot request/response over loopback."""

    def __init__(self, port: int = BRIDGE_PORT, token: str | None = None,
                 timeout: float = IO_TIMEOUT_SECONDS):
        self._port = port
        self._token = token
        self._timeout = timeout

    def _resolve_token(self) -> str:
        if self._token:
            return self._token
        token = read_token()
        if not token:
            raise TokenUnavailable(
                "no bridge token is stored; start the assistant once to create one")
        self._token = token
        return token

    def send(self, request: Request) -> Response:
        """Send one request. Never raises for an unreachable assistant."""
        try:
            token = self._resolve_token()
        except TokenUnavailable as exc:
            return Response(ok=False, error=str(exc),
                            request_id=request.request_id)

        try:
            raw = encode_request(request, token)
        except ProtocolError as exc:
            return Response(ok=False, error=str(exc),
                            request_id=request.request_id)

        try:
            with socket.create_connection((BIND_HOST, self._port),
                                          timeout=self._timeout) as conn:
                conn.settimeout(self._timeout)
                conn.sendall(raw)
                reply = self._read_frame(conn)
        except (OSError, socket.timeout):
            return Response(ok=False, error=OFFLINE_MESSAGE,
                            request_id=request.request_id)

        try:
            return decode_response(reply, token)
        except ProtocolError:
            # An unsigned or mis-signed reply means we are not talking to our
            # assistant. Treat it as unreachable rather than trusting it.
            return Response(ok=False, error=OFFLINE_MESSAGE,
                            request_id=request.request_id)

    @staticmethod
    def _read_frame(conn: socket.socket) -> bytes:
        chunks: list[bytes] = []
        total = 0
        while True:
            part = conn.recv(8192)
            if not part:
                break
            chunks.append(part)
            total += len(part)
            if total > MAX_FRAME_BYTES:
                break
            if b"\n" in part:
                break
        return b"".join(chunks)

    # ---- convenience -----------------------------------------------------

    def ping(self) -> bool:
        return self.send(Request(action="PING")).ok

    def ask(self, target: str, command: str) -> Response:
        return self.send(Request(action="ASK", target=target, command=command))
