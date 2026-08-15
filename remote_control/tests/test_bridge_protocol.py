"""Phase 2 tests: the local bridge protocol.

No sockets are opened here — these test the frame format, authentication,
freshness, and replay rules in isolation. The server itself is exercised in
test_bridge_server.py.
"""
from __future__ import annotations

import json
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control.bridge_protocol import (ACTIONS, BIND_HOST,
                                            MAX_CLOCK_SKEW_SECONDS,
                                            MAX_FRAME_BYTES, NonceCache,
                                            ProtocolError, Request, Response,
                                            decode_request, decode_response,
                                            encode_request, encode_response,
                                            sign)

TOKEN = "a" * 64
OTHER_TOKEN = "b" * 64


def _decode(raw, token=TOKEN, seen=None, now=None):
    return decode_request(raw, token, seen or NonceCache(), now)


class TestBinding(unittest.TestCase):

    def test_binds_only_to_loopback(self):
        self.assertEqual(BIND_HOST, "127.0.0.1")
        self.assertNotEqual(BIND_HOST, "0.0.0.0")


class TestRoundTrip(unittest.TestCase):

    def test_request_survives_encode_decode(self):
        req = Request(action="ASK", target="jarvis", command="what is the CPU load",
                      params={"verbose": True}, request_id="r1")
        got = _decode(encode_request(req, TOKEN))
        self.assertEqual(got.action, "ASK")
        self.assertEqual(got.target, "jarvis")
        self.assertEqual(got.command, "what is the CPU load")
        self.assertEqual(got.params, {"verbose": True})
        self.assertEqual(got.request_id, "r1")

    def test_response_survives_encode_decode(self):
        raw = encode_response(Response(ok=True, text="CPU at 12%",
                                       data={"cpu": 12}, request_id="r1"), TOKEN)
        got = decode_response(raw, TOKEN)
        self.assertTrue(got.ok)
        self.assertEqual(got.text, "CPU at 12%")
        self.assertEqual(got.data, {"cpu": 12})

    def test_request_id_is_generated_when_absent(self):
        raw = encode_request(Request(action="PING"), TOKEN)
        self.assertTrue(_decode(raw).request_id)

    def test_repr_does_not_leak_the_command(self):
        req = Request(action="ASK", command="my password is hunter2")
        self.assertNotIn("hunter2", repr(req))
        self.assertNotIn("hunter2", str(req))


class TestVocabulary(unittest.TestCase):

    def test_unknown_action_cannot_be_sent(self):
        for action in ("SHUTDOWN", "RUN_SHELL", "DELETE_FILE", "", "ping"):
            with self.assertRaises(ProtocolError, msg=action):
                encode_request(Request(action=action), TOKEN)

    def test_unknown_action_cannot_be_received(self):
        body = {"v": 1, "action": "RUN_SHELL", "target": "nexus", "command": "",
                "params": {}, "request_id": "x", "ts": time.time(),
                "nonce": "n" * 32}
        raw = json.dumps({"body": body, "sig": sign(body, TOKEN)}).encode()
        with self.assertRaises(ProtocolError):
            _decode(raw)

    def test_unknown_target_is_refused(self):
        with self.assertRaises(ProtocolError):
            encode_request(Request(action="ASK", target="cortana"), TOKEN)

    def test_phase2_vocabulary_is_read_only(self):
        """Dangerous actions must not exist until their phases pass."""
        for dangerous in ("SHUTDOWN", "RESTART", "SCREENSHOT", "RUN_COMMAND",
                          "DELETE_FILE", "SEND_EMAIL", "POWER_OFF"):
            self.assertNotIn(dangerous, ACTIONS)


class TestAuthentication(unittest.TestCase):

    def test_wrong_token_is_refused(self):
        raw = encode_request(Request(action="PING"), TOKEN)
        with self.assertRaises(ProtocolError):
            _decode(raw, token=OTHER_TOKEN)

    def test_tampered_body_is_refused(self):
        raw = encode_request(Request(action="STATUS"), TOKEN)
        frame = json.loads(raw)
        frame["body"]["action"] = "ASK"
        with self.assertRaises(ProtocolError):
            _decode(json.dumps(frame).encode())

    def test_unsigned_frame_is_refused(self):
        body = {"v": 1, "action": "PING", "target": "nexus", "ts": time.time(),
                "nonce": "n" * 32}
        with self.assertRaises(ProtocolError):
            _decode(json.dumps({"body": body, "sig": ""}).encode())

    def test_token_never_appears_on_the_wire(self):
        raw = encode_request(Request(action="ASK", command="hello"), TOKEN)
        self.assertNotIn(TOKEN.encode(), raw)

    def test_malformed_frames_are_refused(self):
        for raw in (b"", b"not json", b"[]", b'{"body": "x", "sig": "y"}',
                    b'{"sig": "y"}', b"null"):
            with self.assertRaises(ProtocolError, msg=repr(raw)):
                _decode(raw)


class TestFreshness(unittest.TestCase):

    def test_stale_request_is_refused(self):
        raw = encode_request(Request(action="PING"), TOKEN)
        future = time.time() + MAX_CLOCK_SKEW_SECONDS + 10
        with self.assertRaises(ProtocolError):
            _decode(raw, now=future)

    def test_future_dated_request_is_refused(self):
        """Without this, a captured frame stamped far ahead never expires."""
        body = {"v": 1, "action": "PING", "target": "nexus", "command": "",
                "params": {}, "request_id": "x",
                "ts": time.time() + 86400, "nonce": "n" * 32}
        raw = json.dumps({"body": body, "sig": sign(body, TOKEN)}).encode()
        with self.assertRaises(ProtocolError):
            _decode(raw)

    def test_fresh_request_is_accepted(self):
        raw = encode_request(Request(action="PING"), TOKEN)
        self.assertEqual(_decode(raw, now=time.time() + 5).action, "PING")

    def test_missing_timestamp_is_refused(self):
        body = {"v": 1, "action": "PING", "target": "nexus", "command": "",
                "params": {}, "request_id": "x", "nonce": "n" * 32}
        raw = json.dumps({"body": body, "sig": sign(body, TOKEN)}).encode()
        with self.assertRaises(ProtocolError):
            _decode(raw)


class TestReplayProtection(unittest.TestCase):

    def test_same_frame_twice_is_refused(self):
        seen = NonceCache()
        raw = encode_request(Request(action="STATUS"), TOKEN)
        self.assertEqual(decode_request(raw, TOKEN, seen).action, "STATUS")
        with self.assertRaises(ProtocolError):
            decode_request(raw, TOKEN, seen)

    def test_distinct_frames_both_pass(self):
        seen = NonceCache()
        for _ in range(5):
            decode_request(encode_request(Request(action="PING"), TOKEN),
                           TOKEN, seen)

    def test_nonce_is_unique_per_frame(self):
        nonces = {json.loads(encode_request(Request(action="PING"), TOKEN))
                  ["body"]["nonce"] for _ in range(200)}
        self.assertEqual(len(nonces), 200)

    def test_short_nonce_is_refused(self):
        body = {"v": 1, "action": "PING", "target": "nexus", "command": "",
                "params": {}, "request_id": "x", "ts": time.time(), "nonce": "ab"}
        raw = json.dumps({"body": body, "sig": sign(body, TOKEN)}).encode()
        with self.assertRaises(ProtocolError):
            _decode(raw)

    def test_cache_forgets_nonces_outside_the_window(self):
        seen = NonceCache(window_seconds=10)
        seen.add("old", 1000.0)
        seen.add("new", 1100.0)      # evicts anything older than 1090
        self.assertEqual(len(seen), 1)

    def test_cache_does_not_grow_without_bound(self):
        seen = NonceCache()
        now = time.time()
        for i in range(12_000):
            seen.add(f"n{i}", now)
            self.assertLessEqual(len(seen), 10_001)


class TestSizeLimits(unittest.TestCase):

    def test_oversized_request_is_refused_at_encode(self):
        with self.assertRaises(ProtocolError):
            encode_request(Request(action="ASK", command="x" * MAX_FRAME_BYTES),
                           TOKEN)

    def test_oversized_frame_is_refused_at_decode(self):
        with self.assertRaises(ProtocolError):
            _decode(b"x" * (MAX_FRAME_BYTES + 1))

    def test_oversized_response_is_truncated_not_dropped(self):
        raw = encode_response(Response(ok=True, text="y" * 200_000), TOKEN)
        self.assertLessEqual(len(raw), MAX_FRAME_BYTES)
        got = decode_response(raw, TOKEN)
        self.assertTrue(got.ok)
        self.assertIn("[truncated]", got.text)


class TestResponseAuthentication(unittest.TestCase):

    def test_forged_response_is_refused(self):
        raw = encode_response(Response(ok=True, text="fine"), OTHER_TOKEN)
        with self.assertRaises(ProtocolError):
            decode_response(raw, TOKEN)


if __name__ == "__main__":
    unittest.main(verbosity=2)
