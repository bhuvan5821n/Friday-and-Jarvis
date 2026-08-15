"""Phase 11 tests: the executor.

The claim: no dangerous action runs without a matching, unexpired, single-use
confirmation, and ordinary conversation never enters this path.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import audit, commands, executor


def setUpModule():
    global _dir, _patches
    _dir = tempfile.TemporaryDirectory()
    path = Path(_dir.name) / "audit.jsonl"
    _patches = [mock.patch.object(audit, "AUDIT_PATH", path),
                mock.patch.object(audit, "_RUNTIME", path.parent)]
    for patch in _patches:
        patch.start()


def tearDownModule():
    for patch in _patches:
        patch.stop()
    _dir.cleanup()


class _Base(unittest.TestCase):

    def setUp(self):
        executor.manager.clear()
        # Stop this patcher specifically, not `mock.patch.stopall()`: stopall
        # would also tear down the module-level audit redirection and let the
        # tests write into the real remote log.
        patcher = mock.patch.object(
            commands.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout="done", stderr=""))
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)


class TestDangerousActions(_Base):

    def test_a_dangerous_command_asks_before_it_acts(self):
        reply = executor.handle("shutdown")
        self.assertIn("CONFIRM", reply)
        self.assertFalse(self.spawn.called, "it ran without confirmation")

    def test_confirming_runs_it(self):
        prompt = executor.handle("shutdown")
        token = prompt.split("CONFIRM ")[1].split()[0]
        executor.handle(f"CONFIRM {token}")
        self.assertTrue(self.spawn.called)
        self.assertEqual(self.spawn.call_args.args[0][0], "shutdown.exe")

    def test_a_bare_confirm_works_because_only_one_can_be_pending(self):
        executor.handle("restart")
        executor.handle("confirm")
        self.assertTrue(self.spawn.called)

    def test_cancelling_does_not_run_it(self):
        executor.handle("shutdown")
        reply = executor.handle("cancel")
        self.assertIn("not done", reply)
        self.assertFalse(self.spawn.called)

    def test_a_wrong_token_does_not_run_it(self):
        executor.handle("shutdown")
        executor.handle("CONFIRM zzzz99")
        self.assertFalse(self.spawn.called)

    def test_a_replayed_confirmation_runs_it_only_once(self):
        prompt = executor.handle("shutdown")
        token = prompt.split("CONFIRM ")[1].split()[0]
        executor.handle(f"CONFIRM {token}")
        executor.handle(f"CONFIRM {token}")
        self.assertEqual(self.spawn.call_count, 1)

    def test_confirming_nothing_is_refused(self):
        self.assertIn("nothing waiting", executor.handle("confirm"))
        self.assertFalse(self.spawn.called)

    def test_a_newer_request_supersedes_the_older_one(self):
        executor.handle("shutdown")
        prompt = executor.handle("restart")
        token = prompt.split("CONFIRM ")[1].split()[0]
        executor.handle(f"CONFIRM {token}")
        self.assertIn("/r", self.spawn.call_args.args[0])


class TestSafeActions(_Base):

    def test_a_safe_command_runs_immediately(self):
        executor.handle("lock")
        self.assertTrue(self.spawn.called)

    def test_the_command_list_is_available(self):
        for phrasing in ("commands", "help", "what can you do"):
            self.assertIn("shutdown", executor.handle(phrasing), phrasing)


class TestOrdinaryConversationIsNotACommand(_Base):

    def test_conversation_falls_through_to_the_assistant(self):
        for text in ("how are you", "what is the capital of France",
                     "remind me to call mom", "tell me a joke", ""):
            self.assertIsNone(executor.handle(text), text)
        self.assertFalse(self.spawn.called)

    def test_shell_text_never_runs(self):
        for attempt in ("powershell -c whoami", "cmd /c del *.*",
                        "format C: /y", "rm -rf ~"):
            self.assertIsNone(executor.handle(attempt), attempt)
        self.assertFalse(self.spawn.called)

    def test_the_shell_refusal_is_the_exact_required_sentence(self):
        self.assertEqual(
            executor.refuse_shell("powershell whoami"),
            "I cannot execute arbitrary remote shell text. I can run approved "
            "commands or a locally reviewed command macro.")


class TestScreenshots(_Base):

    def setUp(self):
        super().setUp()
        executor.set_image_sender(None)
        self.addCleanup(executor.set_image_sender, None)
        patcher = mock.patch("remote_control.screenshot.capture",
                             return_value=(b"\x89PNG",
                                           {"width": 1280, "height": 720,
                                            "bytes": 4, "ms": 10}))
        self.capture = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_screenshot_is_asked_for_in_plain_english(self):
        for phrasing in ("screenshot", "take a screenshot", "show me the screen",
                         "what's on screen"):
            self.capture.reset_mock()
            self.assertIsNotNone(executor.handle(phrasing), phrasing)
            self.assertTrue(self.capture.called, phrasing)

    def test_the_image_is_delivered_through_the_registered_sender(self):
        sent = []
        executor.set_image_sender(lambda data, caption: sent.append(data) or True)
        reply = executor.handle("screenshot")
        self.assertEqual(sent, [b"\x89PNG"])
        self.assertIn("1280x720", reply)

    def test_a_secure_screen_is_refused_and_nothing_is_sent(self):
        from remote_control.screenshot import ScreenshotError
        self.capture.side_effect = ScreenshotError("secure screen")
        sent = []
        executor.set_image_sender(lambda d, c: sent.append(d) or True)
        reply = executor.handle("screenshot")
        self.assertEqual(sent, [])
        self.assertIn("secure screen", reply)

    def test_a_failed_delivery_says_so_rather_than_claiming_success(self):
        executor.set_image_sender(lambda d, c: False)
        self.assertIn("could not attach", executor.handle("screenshot"))

    def test_the_audit_records_size_not_pixels(self):
        executor.set_image_sender(lambda d, c: True)
        with mock.patch.object(audit, "record") as rec:
            executor.handle("screenshot")
        self.assertEqual(rec.call_args.args[0], "screenshot")
        self.assertNotIn("PNG", repr(rec.call_args))


class TestAuditing(_Base):

    def test_every_outcome_is_recorded(self):
        with mock.patch.object(audit, "record") as rec:
            executor.handle("lock")
            executor.handle("shutdown")
            executor.handle("cancel")
        actions = [c.args[0] for c in rec.call_args_list]
        self.assertEqual(actions, ["lock", "shutdown", "shutdown"])
        self.assertEqual(rec.call_args_list[-1].args[1], "cancelled")

    def test_a_confirmed_run_is_marked_confirmed(self):
        prompt = executor.handle("shutdown")
        token = prompt.split("CONFIRM ")[1].split()[0]
        with mock.patch.object(audit, "record") as rec:
            executor.handle(f"CONFIRM {token}")
        self.assertTrue(rec.call_args.kwargs["confirmed"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
