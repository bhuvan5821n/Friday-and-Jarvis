"""Phase 14 tests: the audit log.

The log is where privacy leaks would accumulate quietly, so most of these tests
are about what must *not* appear in the file.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import audit


class _TempLog(unittest.TestCase):
    """Each test writes to its own file, never the real runtime log."""

    def setUp(self):
        import tempfile
        self._dir = tempfile.TemporaryDirectory()
        self.path = Path(self._dir.name) / "audit.jsonl"
        patches = [mock.patch.object(audit, "AUDIT_PATH", self.path),
                   mock.patch.object(audit, "_RUNTIME", self.path.parent)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.addCleanup(self._dir.cleanup)

    def written(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [json.loads(l) for l in
                self.path.read_text(encoding="utf-8").splitlines() if l.strip()]


class TestRecording(_TempLog):

    def test_an_action_is_recorded(self):
        audit.record("screenshot", "ok", target="jarvis", detail="1 image sent")
        entries = self.written()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["action"], "screenshot")
        self.assertEqual(entries[0]["outcome"], "ok")
        self.assertEqual(entries[0]["target"], "jarvis")

    def test_records_append_rather_than_replace(self):
        for i in range(5):
            audit.record(f"action{i}", "ok")
        self.assertEqual(len(self.written()), 5)

    def test_only_the_known_fields_are_written(self):
        audit.record("shutdown", "confirmed", confirmed=True, duration_ms=12)
        self.assertEqual(set(self.written()[0]), set(audit.FIELDS))

    def test_an_unknown_outcome_becomes_failed_not_invented(self):
        audit.record("thing", "probably fine")
        self.assertEqual(self.written()[0]["outcome"], "failed")

    def test_a_confirmed_action_is_marked_as_confirmed(self):
        audit.record("shutdown", "confirmed", confirmed=True)
        self.assertTrue(self.written()[0]["confirmed"])


class TestPrivacy(_TempLog):
    """What must never reach the file."""

    def test_secret_shaped_values_are_scrubbed(self):
        secrets_in = [
            "a" * 64,
            "Bearer abc123def456ghi789jkl",
            "token=9f8e7d6c5b4a3f2e1d0c9b8a7f6e5d4c",
            "AIzaSyD-1234567890abcdefghijklmnop",
            "sk-proj-abcdefghijklmnopqrstuvwxyz",
            "password: hunter2",
        ]
        for value in secrets_in:
            scrubbed = audit.scrub(value)
            self.assertIn("[redacted]", scrubbed, value)

    def test_a_leaked_bridge_token_would_not_be_written(self):
        from remote_control.security.token_store import get_or_create_token
        token = get_or_create_token()
        audit.record("ask", "ok", detail=f"call failed with {token}")
        self.assertNotIn(token, self.path.read_text(encoding="utf-8"))

    def test_detail_is_clamped_so_bodies_cannot_be_smuggled_in(self):
        audit.record("email", "ok", detail="x" * 5000)
        self.assertLessEqual(len(self.written()[0]["detail"]), 210)

    def test_the_module_has_no_way_to_log_a_message_body(self):
        """There is no `message`, `body`, or `content` field to put one in."""
        for forbidden in ("message", "body", "content", "text", "reply"):
            self.assertNotIn(forbidden, audit.FIELDS, forbidden)

    def test_extra_keyword_arguments_are_refused_not_serialized(self):
        with self.assertRaises(TypeError):
            audit.record("ask", "ok", message="buy milk")


class TestResilience(_TempLog):

    def test_a_write_failure_does_not_raise(self):
        with mock.patch.object(Path, "open", side_effect=OSError("disk full")):
            entry = audit.record("shutdown", "ok")
        self.assertEqual(entry["action"], "shutdown")

    def test_the_log_rotates_instead_of_growing_forever(self):
        with mock.patch.object(audit, "MAX_BYTES", 500):
            for i in range(60):
                audit.record(f"action{i}", "ok", detail="padding" * 5)
        self.assertLess(self.path.stat().st_size, 2000)
        self.assertTrue(self.path.with_suffix(".jsonl.1").exists())

    def test_a_corrupt_line_does_not_break_reading(self):
        audit.record("good", "ok")
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write("{not json\n")
        audit.record("also_good", "ok")
        self.assertEqual([e["action"] for e in audit.recent()],
                         ["good", "also_good"])

    def test_reading_an_absent_log_is_empty_not_an_error(self):
        self.assertEqual(audit.recent(), [])
        self.assertIn("No remote actions", audit.format_recent())


class TestReporting(_TempLog):

    def test_recent_returns_newest_last_and_respects_the_limit(self):
        for i in range(30):
            audit.record(f"action{i}", "ok")
        entries = audit.recent(5)
        self.assertEqual(len(entries), 5)
        self.assertEqual(entries[-1]["action"], "action29")

    def test_the_summary_is_phone_sized_and_readable(self):
        audit.record("screenshot", "ok", detail="1 image")
        audit.record("shutdown", "refused")
        text = audit.format_recent()
        self.assertIn("screenshot", text)
        self.assertIn("shutdown", text)
        self.assertLess(len(text), 1000)


class TestNoCapabilityOfItsOwn(unittest.TestCase):

    def test_the_audit_module_cannot_act_on_the_machine(self):
        source = Path(audit.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "Popen", "os.system", "shutil.rmtree",
                          "eval(", "exec(", "socket"):
            self.assertNotIn(forbidden, source, forbidden)

    def test_the_log_lives_in_gitignored_runtime(self):
        repo = Path(__file__).resolve().parents[2]
        self.assertEqual(audit.AUDIT_PATH.parent, repo / "runtime")
        self.assertIn("runtime/",
                      (repo / ".gitignore").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
