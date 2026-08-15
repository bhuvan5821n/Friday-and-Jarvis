"""Phase 2 tests: the process-level bridge service.

These use the real Credential Manager token (the same one the assistant uses)
and bind the real bridge port, so they verify the wiring the app actually runs
— not a stand-in. Each test leaves the port free.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import service
from remote_control.bridge_client import BridgeClient
from remote_control.bridge_protocol import BRIDGE_PORT, Request, Response


class TestService(unittest.TestCase):

    def setUp(self):
        service.shutdown_service()
        self.server = service.start_bridge()
        if self.server is None:
            self.skipTest("bridge could not start (port busy or no token)")

    def tearDown(self):
        service.shutdown_service()

    def test_ping_answers_over_the_real_port(self):
        self.assertEqual(self.server.port, BRIDGE_PORT)
        self.assertTrue(BridgeClient().ping())

    def test_start_is_idempotent(self):
        self.assertIs(service.start_bridge(), self.server)

    def test_status_is_answered_from_local_collectors(self):
        reply = BridgeClient().send(Request(action="STATUS"))
        self.assertTrue(reply.ok)
        self.assertIn("CPU:", reply.text)
        self.assertIn("temperature_text", reply.data)

    def test_whats_happening_is_answered(self):
        reply = BridgeClient().send(Request(action="WHATS_HAPPENING"))
        self.assertTrue(reply.ok)
        self.assertTrue(reply.text)

    def test_ask_reaches_the_router(self):
        with mock.patch("remote_control.router.route",
                        return_value=Response(ok=True, text="routed")) as m:
            reply = BridgeClient().send(Request(action="ASK", target="friday",
                                                command="hello"))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.text, "routed")
        self.assertEqual(m.call_args.args[0].target, "friday")

    def test_shutdown_closes_the_port(self):
        report = service.shutdown_service()
        self.assertTrue(report["bridge_stopped"])
        self.assertFalse(service.is_running())
        self.assertFalse(BridgeClient(timeout=1).ping())

    def test_shutdown_is_safe_when_never_started(self):
        service.shutdown_service()
        self.assertEqual(service.shutdown_service(),
                         {"bridge_stopped": False, "requests_served": 0})


class TestTokenStore(unittest.TestCase):

    def test_token_is_stable_and_not_in_the_repo(self):
        from remote_control.security.token_store import get_or_create_token
        token = get_or_create_token()
        self.assertEqual(token, get_or_create_token())
        self.assertGreaterEqual(len(token), 32)

        repo = Path(__file__).resolve().parents[2]
        for path in list(repo.glob("config/*.json")) + list(
                repo.glob("remote_control/**/*.py")):
            self.assertNotIn(token, path.read_text(encoding="utf-8",
                                                   errors="ignore"), str(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
