"""Phase 2 tests: the bridge server and client over a real loopback socket.

These open real sockets on an ephemeral port. Nothing binds to a routable
interface, nothing touches Credential Manager (a fixed test token is injected),
and no assistant subsystem is involved — the handler is a stub.
"""
from __future__ import annotations

import socket
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control.bridge_client import OFFLINE_MESSAGE, BridgeClient
from remote_control.bridge_protocol import (BIND_HOST, Request, Response,
                                            encode_request)
from remote_control.bridge_server import BridgeServer

TOKEN = "c" * 64
WRONG_TOKEN = "d" * 64


def _free_port() -> int:
    with socket.socket() as s:
        s.bind((BIND_HOST, 0))
        return s.getsockname()[1]


def _echo_handler(request: Request) -> Response:
    if request.action == "PING":
        return Response(ok=True, text="pong")
    return Response(ok=True, text=f"{request.target}:{request.command}",
                    data={"action": request.action})


class _ServerCase(unittest.TestCase):

    handler = staticmethod(_echo_handler)

    def setUp(self):
        self.port = _free_port()
        self.server = BridgeServer(self.handler, port=self.port, token=TOKEN)
        self.server.start()
        self.client = BridgeClient(port=self.port, token=TOKEN, timeout=5)

    def tearDown(self):
        self.server.stop()


class TestServing(_ServerCase):

    def test_ping_round_trip(self):
        reply = self.client.send(Request(action="PING"))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "pong")

    def test_ask_is_routed_with_target_and_command(self):
        reply = self.client.ask("jarvis", "what is the battery level")
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "jarvis:what is the battery level")

    def test_request_id_is_echoed_back(self):
        reply = self.client.send(Request(action="STATUS", request_id="abc123"))
        self.assertEqual(reply.request_id, "abc123")

    def test_many_sequential_requests(self):
        for _ in range(25):
            self.assertTrue(self.client.ping())
        self.assertEqual(self.server.requests_served, 25)

    def test_concurrent_requests(self):
        results = []
        def hit():
            results.append(self.client.ping())
        threads = [threading.Thread(target=hit) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        self.assertEqual(results, [True] * 10)


class TestBinding(_ServerCase):

    def test_listens_on_loopback_only(self):
        with socket.create_connection((BIND_HOST, self.port), timeout=2):
            pass    # loopback works

        # The bind address is 127.0.0.1, so no other local interface accepts.
        host_ip = socket.gethostbyname(socket.gethostname())
        if host_ip.startswith("127."):
            self.skipTest("host resolves to loopback; nothing else to test")
        with self.assertRaises(OSError):
            socket.create_connection((host_ip, self.port), timeout=2)

    def test_port_is_distinct_from_existing_channels(self):
        from remote_control.bridge_protocol import BRIDGE_PORT
        self.assertNotIn(BRIDGE_PORT, (48757, 48800))


class TestAuthentication(_ServerCase):

    def test_wrong_token_is_rejected(self):
        bad = BridgeClient(port=self.port, token=WRONG_TOKEN, timeout=5)
        reply = bad.send(Request(action="PING"))
        self.assertFalse(reply.ok)
        self.assertEqual(self.server.requests_served, 0)
        self.assertEqual(self.server.requests_rejected, 1)

    def test_unsigned_garbage_is_rejected(self):
        with socket.create_connection((BIND_HOST, self.port), timeout=5) as c:
            c.sendall(b'{"body": {"action": "PING"}, "sig": "x"}\n')
            c.recv(8192)
        self.assertEqual(self.server.requests_rejected, 1)

    def test_rejection_reason_is_not_disclosed(self):
        """A client must not learn *why* it was refused."""
        bad = BridgeClient(port=self.port, token=WRONG_TOKEN, timeout=5)
        reply = bad.send(Request(action="PING"))
        for leak in ("signature", "nonce", "replay", "token", "stale"):
            self.assertNotIn(leak, reply.error.lower())

    def test_replayed_frame_is_rejected(self):
        raw = encode_request(Request(action="PING"), TOKEN)
        for expected in (True, False):
            with socket.create_connection((BIND_HOST, self.port), timeout=5) as c:
                c.sendall(raw)
                reply = c.recv(8192)
            self.assertEqual(b'"ok": true' in reply.replace(b'"ok":true',
                                                            b'"ok": true'),
                             expected, f"replay accepted on pass {expected}")


class TestFailureHandling(unittest.TestCase):

    def test_offline_assistant_reports_honestly(self):
        client = BridgeClient(port=_free_port(), token=TOKEN, timeout=1)
        reply = client.send(Request(action="PING"))
        self.assertFalse(reply.ok)
        self.assertEqual(reply.error, OFFLINE_MESSAGE)
        # It must not claim to know why, or leak socket details.
        self.assertNotIn("refused", reply.error.lower())

    def test_handler_exception_does_not_kill_the_server(self):
        def explode(request):
            raise RuntimeError("handler blew up")

        port = _free_port()
        server = BridgeServer(explode, port=port, token=TOKEN)
        server.start()
        try:
            client = BridgeClient(port=port, token=TOKEN, timeout=5)
            reply = client.send(Request(action="PING"))
            self.assertFalse(reply.ok)
            self.assertNotIn("blew up", reply.error)   # no internals leaked
            self.assertTrue(server.running)
        finally:
            server.stop()

    def test_client_never_hangs_on_a_silent_server(self):
        """A server that accepts but never replies must time out, not hang."""
        listener = socket.socket()
        listener.bind((BIND_HOST, 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        try:
            client = BridgeClient(port=port, token=TOKEN, timeout=1)
            start = time.monotonic()
            reply = client.send(Request(action="PING"))
            self.assertFalse(reply.ok)
            self.assertLess(time.monotonic() - start, 10)
        finally:
            listener.close()

    def test_no_newline_flood_is_bounded(self):
        """A client that never terminates its frame must not exhaust memory."""
        port = _free_port()
        server = BridgeServer(_echo_handler, port=port, token=TOKEN)
        server.start()
        try:
            with socket.create_connection((BIND_HOST, port), timeout=5) as c:
                try:
                    for _ in range(40):
                        c.sendall(b"x" * 8192)      # 320 KB, no newline
                except OSError:
                    pass                            # server hung up: correct
            self.assertTrue(server.running)
        finally:
            server.stop()


class TestLifecycle(unittest.TestCase):

    def test_start_stop_releases_the_port(self):
        port = _free_port()
        server = BridgeServer(_echo_handler, port=port, token=TOKEN)
        server.start()
        self.assertTrue(server.running)
        server.stop()
        self.assertFalse(server.running)
        # The port must be reusable immediately after a clean stop.
        again = BridgeServer(_echo_handler, port=port, token=TOKEN)
        again.start()
        try:
            self.assertTrue(BridgeClient(port=port, token=TOKEN, timeout=5).ping())
        finally:
            again.stop()

    def test_stop_is_idempotent(self):
        server = BridgeServer(_echo_handler, port=_free_port(), token=TOKEN)
        server.start()
        server.stop()
        server.stop()

    def test_second_bind_on_same_port_fails_loudly(self):
        """Two control channels on one port would be ambiguous — refuse."""
        port = _free_port()
        first = BridgeServer(_echo_handler, port=port, token=TOKEN)
        first.start()
        try:
            second = BridgeServer(_echo_handler, port=port, token=TOKEN)
            with self.assertRaises(OSError):
                second.start()
        finally:
            first.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
