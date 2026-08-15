"""Assistant-side lifecycle IPC server.

Replaces the single-purpose ``SHOW`` listener in ``core.runtime`` with the
authenticated command channel the launcher speaks (see launcher/protocol.py),
on the same port — so a legacy ``SHOW`` still works, and only one instance can
ever hold the socket (bind-to-claim, no SO_REUSEADDR).

Every handler runs through callbacks supplied by the app; this module owns no
widgets and touches Qt only through those callbacks, which are expected to be
thread-safe (signal emitters).
"""
from __future__ import annotations

import json
import logging
import socket
import threading

from launcher.protocol import IPC_PORT, decode, get_token

log = logging.getLogger("lifecycle.server")


class LifecycleServer:
    """Bind-to-claim single instance + validated lifecycle commands."""

    def __init__(self, handlers: dict):
        """``handlers`` maps command name -> callable(payload dict) -> dict.

        Missing handlers answer ``{"error": "unsupported"}`` rather than
        crashing, so a partial integration still runs.
        """
        self._handlers = dict(handlers)
        self._token = get_token()
        self._srv: socket.socket | None = None
        self.state = "STARTING"

    # -- single instance ---------------------------------------------------

    def claim(self) -> bool:
        """True = we are the one instance and now serve lifecycle commands."""
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
                srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            srv.bind(("127.0.0.1", IPC_PORT))
            srv.listen(4)
        except OSError:
            return False
        self._srv = srv
        threading.Thread(target=self._serve, daemon=True,
                         name="LifecycleIPC").start()
        return True

    def set_state(self, state: str) -> None:
        self.state = state
        log.info("lifecycle state -> %s", state)

    # -- serving -------------------------------------------------------------

    def _serve(self) -> None:
        while True:
            try:
                conn, _ = self._srv.accept()
                threading.Thread(target=self._handle, args=(conn,),
                                 daemon=True).start()
            except OSError:
                return  # socket closed during shutdown
            except Exception as exc:
                log.warning("accept failed: %s", exc)

    def _handle(self, conn: socket.socket) -> None:
        with conn:
            try:
                conn.settimeout(3.0)
                raw = conn.recv(4096)
                body = decode(raw, self._token)
                if body is None:
                    conn.sendall(b'{"error": "rejected"}\n')
                    return
                command = body["command"]
                log.info("lifecycle command: %s", command)

                if command == "PING":
                    reply = {"state": self.state}
                else:
                    handler = self._handlers.get(command)
                    if handler is None:
                        reply = {"error": "unsupported"}
                    else:
                        reply = handler(body) or {}
                        reply.setdefault("ok", "error" not in reply)
                conn.sendall((json.dumps(reply) + "\n").encode("utf-8"))
            except Exception as exc:
                log.warning("request failed: %s", exc)
                try:
                    conn.sendall(b'{"error": "internal"}\n')
                except Exception:
                    pass

    def close(self) -> None:
        try:
            if self._srv is not None:
                self._srv.close()
        except Exception:
            pass


def demo() -> None:
    """Self-check: claim, authenticated round-trip, reject bad token."""
    import time
    from launcher.protocol import send, encode

    calls = []
    server = LifecycleServer({
        "SHOW_WINDOW": lambda body: (calls.append(body), {"shown": True})[1],
    })
    assert server.claim(), "port busy — is the assistant running? Stop it first."
    server.set_state("READY")
    time.sleep(0.2)

    assert send("PING", port=IPC_PORT) == {"state": "READY"}
    assert send("SHOW_WINDOW", persona="friday", timeout=2.0,
                port=IPC_PORT)["shown"] is True
    assert calls and calls[0].get("persona") == "friday"
    assert send("SHUTDOWN", port=IPC_PORT) == {"error": "unsupported"}, \
        "no handler => refuse"

    # Wrong token must be rejected outright.
    with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as c:
        bad = encode("SHUTDOWN", "not-the-token")
        c.sendall(bad)
        assert b"rejected" in c.recv(64)

    # Legacy bare SHOW still maps to SHOW_WINDOW.
    with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as c:
        c.sendall(b"SHOW")
        assert b"shown" in c.recv(64)

    server.close()
    print("lifecycle_server demo OK")


if __name__ == "__main__":
    demo()
