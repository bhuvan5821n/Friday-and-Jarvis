"""Process-level ownership of the NEXUS bridge.

One bridge per assistant process, started at boot and stopped as part of the
existing complete-shutdown path. Follows the same shape as
`services.web_intelligence.tool.shutdown_service`: module-level singleton,
safe to shut down when it was never started.

Phase 2 shipped the transport; Phase 3 answers STATUS and WHATS_HAPPENING from
the collectors that already run; Phase 4 routes ASK to JARVIS, FRIDAY, or
NEXUS. Every action in the protocol vocabulary now has a real handler, and the
vocabulary itself is what bounds this: an action the protocol does not list is
refused before it reaches here.
"""
from __future__ import annotations

import logging
import threading

from .bridge_protocol import Request, Response
from .bridge_server import BridgeServer
from .security.token_store import TokenUnavailable

log = logging.getLogger("nexus.service")

_server: BridgeServer | None = None
_lock = threading.Lock()


def _handle(request: Request) -> Response:
    if request.action == "PING":
        return Response(ok=True, text="pong", data={"target": request.target})

    if request.action == "STATUS":
        from .status import format_status, snapshot
        data = snapshot()
        return Response(ok=True, text=format_status(data), data=data)

    if request.action == "WHATS_HAPPENING":
        from .status import whats_happening
        return Response(ok=True, text=whats_happening())

    if request.action == "AUDIT":
        from .audit import format_recent
        return Response(ok=True, text=format_recent())

    if request.action == "ASK":
        from .router import route
        return route(request)

    # decode_request already refuses anything outside ACTIONS, so reaching here
    # means the vocabulary grew without a handler.
    log.error("no handler for action %r", request.action)
    return Response(ok=False, request_id=request.request_id,
                    error="That capability is not available on this build.")


def start_bridge() -> BridgeServer | None:
    """Start the loopback bridge. Returns None if it could not start.

    A failure here must never stop the assistant from booting: local voice
    conversation does not depend on remote control.
    """
    global _server
    with _lock:
        if _server is not None and _server.running:
            return _server
        try:
            server = BridgeServer(_handle)
            server.start()
        except TokenUnavailable as exc:
            log.error("NEXUS bridge not started (no bridge token): %s", exc)
            return None
        except OSError as exc:
            log.error("NEXUS bridge not started (port unavailable): %s", exc)
            return None
        _server = server
        return server


def shutdown_service() -> dict:
    """Called from the app's shutdown path. Safe when never started."""
    global _server
    with _lock:
        if _server is None:
            return {"bridge_stopped": False, "requests_served": 0}
        served = _server.requests_served
        _server.stop()
        _server = None
        return {"bridge_stopped": True, "requests_served": served}


def is_running() -> bool:
    return _server is not None and _server.running
