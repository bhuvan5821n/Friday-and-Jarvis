"""The bridge server — the assistant's only remote-control entry point.

Runs inside the JARVIS/FRIDAY process on a loopback-only socket. The Hermes
gateway connects here after its own admission gate has already decided the
message was addressed to an assistant by the authorized sender; this layer
re-checks everything it can check locally (signature, freshness, replay,
vocabulary) because a gate on the other side of a socket is a claim, not a
guarantee.

One connection, one request, one response, then close. No session state, no
streaming, no keep-alive: a remote control surface should be as small as it
can be.
"""
from __future__ import annotations

import logging
import socket
import threading
from typing import Callable

from .bridge_protocol import (BIND_HOST, BRIDGE_PORT, IO_TIMEOUT_SECONDS,
                              MAX_FRAME_BYTES, NonceCache, ProtocolError,
                              Request, Response, decode_request,
                              encode_response)
from .security.token_store import TokenUnavailable, get_or_create_token

log = logging.getLogger("nexus.bridge")

#: Sent for every rejection, regardless of cause. A client that can tell "bad
#: signature" from "replayed nonce" learns something about the secret.
_REJECTED = "rejected"


class BridgeServer:
    """Accepts authenticated local requests and hands them to a handler.

    The handler is supplied by the assistant and is the only place that knows
    how to actually do anything. This class owns the socket, the auth, and the
    error boundary — nothing else.
    """

    def __init__(self, handler: Callable[[Request], Response],
                 port: int = BRIDGE_PORT, token: str | None = None):
        self._handler = handler
        self._port = port
        self._token = token
        self._sock: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._nonces = NonceCache()
        self._lock = threading.Lock()
        self.requests_served = 0
        self.requests_rejected = 0

    # ---- lifecycle -------------------------------------------------------

    def start(self) -> int:
        """Bind, listen, and serve on a daemon thread. Returns the real port.

        Raises rather than serving unauthenticated if no token is available:
        an open remote-control socket is worse than none at all.
        """
        if self._token is None:
            self._token = get_or_create_token()   # raises TokenUnavailable

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # Deliberately no SO_REUSEADDR: if something already holds this port we
        # want to fail loudly, not quietly share a control channel.
        sock.bind((BIND_HOST, self._port))
        sock.listen(8)
        sock.settimeout(0.5)          # so stop() is responsive
        self._sock = sock
        self._port = sock.getsockname()[1]

        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name="nexus-bridge",
                                        daemon=True)
        self._thread.start()
        log.info("NEXUS bridge listening on %s:%s", BIND_HOST, self._port)
        return self._port

    def stop(self, timeout: float = 3.0) -> None:
        """Stop serving and close the socket. Safe to call twice."""
        self._stop.set()
        sock, self._sock = self._sock, None
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        thread, self._thread = self._thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        log.info("NEXUS bridge stopped")

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def port(self) -> int:
        return self._port

    # ---- serving ---------------------------------------------------------

    def _serve(self) -> None:
        while not self._stop.is_set():
            sock = self._sock
            if sock is None:
                break
            try:
                conn, addr = sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break                 # socket closed by stop()
            # Belt and braces: the bind is loopback-only, so this should be
            # unreachable. If it ever fires, something is very wrong.
            if addr[0] != BIND_HOST:
                log.error("refusing non-loopback connection from %s", addr[0])
                try:
                    conn.close()
                except Exception:
                    pass
                continue
            threading.Thread(target=self._handle_connection, args=(conn,),
                             daemon=True).start()

    def _handle_connection(self, conn: socket.socket) -> None:
        try:
            conn.settimeout(IO_TIMEOUT_SECONDS)
            raw = self._read_frame(conn)
            reply = self._process(raw)
            conn.sendall(encode_response(reply, self._token))
        except Exception:
            log.debug("bridge connection failed", exc_info=True)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    def _read_frame(self, conn: socket.socket) -> bytes:
        """One newline-terminated frame, size-capped.

        Reads in a loop because a single recv() is not guaranteed to return a
        whole frame; caps total bytes so a client that never sends a newline
        cannot make us buffer forever.
        """
        chunks: list[bytes] = []
        total = 0
        while True:
            part = conn.recv(8192)
            if not part:
                break
            chunks.append(part)
            total += len(part)
            if total > MAX_FRAME_BYTES:
                raise ProtocolError("frame too large")
            if b"\n" in part:
                break
        return b"".join(chunks)

    def _process(self, raw: bytes) -> Response:
        try:
            with self._lock:
                request = decode_request(raw, self._token, self._nonces)
        except ProtocolError as exc:
            with self._lock:
                self.requests_rejected += 1
            # The reason is logged locally but never returned to the client.
            log.warning("bridge rejected a request: %s", exc)
            return Response(ok=False, error=_REJECTED)

        try:
            reply = self._handler(request)
        except Exception:
            log.exception("bridge handler failed")
            return Response(ok=False, error="the assistant could not complete "
                                            "that request",
                            request_id=request.request_id)
        with self._lock:
            self.requests_served += 1
        if not isinstance(reply, Response):
            log.error("handler returned %s, not a Response", type(reply).__name__)
            return Response(ok=False, error="internal error",
                            request_id=request.request_id)
        # Stamp the id so a client can correlate even if the handler forgot.
        if not reply.request_id:
            reply = Response(ok=reply.ok, text=reply.text, data=reply.data,
                             error=reply.error, request_id=request.request_id)
        return reply
