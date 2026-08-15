"""Phase 9 tests: the confirmation state machine.

No dangerous action exists yet — that is the point. This phase must pass before
any capability that can change the machine is built, so these tests define the
contract those capabilities will be held to.
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import confirmation as confirm_mod
from remote_control.confirmation import (EXPIRY_SECONDS, ConfirmationError,
                                         ConfirmationManager, PendingAction,
                                         parse_confirmation)


class TestParsing(unittest.TestCase):

    def test_confirmation_phrases_are_recognized(self):
        for text in ("CONFIRM", "confirm", "/confirm", "yes", "Yes.",
                     "do it", "go ahead", "proceed"):
            self.assertEqual(parse_confirmation(text)[0], "confirm", text)

    def test_cancellation_phrases_are_recognized(self):
        for text in ("CANCEL", "cancel", "/cancel", "no", "stop", "abort",
                     "nevermind", "never mind"):
            self.assertEqual(parse_confirmation(text)[0], "cancel", text)

    def test_a_token_is_extracted_when_given(self):
        self.assertEqual(parse_confirmation("CONFIRM a1b2c3"), ("confirm", "a1b2c3"))
        self.assertEqual(parse_confirmation("cancel a1b2c3"), ("cancel", "a1b2c3"))

    def test_ordinary_messages_are_not_confirmations(self):
        for text in ("shut down the laptop", "yes I agree with that plan",
                     "no idea what happened", "confirmation email arrived",
                     "", "   ", "cancel my subscription tomorrow"):
            self.assertIsNone(parse_confirmation(text)[0], text)


class TestLifecycle(unittest.TestCase):

    def setUp(self):
        self.mgr = ConfirmationManager()

    def test_a_request_is_not_executed_by_asking(self):
        pending = self.mgr.request("shutdown", "Shut down the laptop?")
        self.assertIs(self.mgr.pending, pending)
        self.assertTrue(pending.token)

    def test_the_prompt_tells_bhuvan_exactly_what_to_reply(self):
        pending = self.mgr.request("shutdown", "Shut down the laptop?")
        text = self.mgr.prompt_text(pending)
        self.assertIn("Shut down the laptop?", text)
        self.assertIn(f"CONFIRM {pending.token}", text)
        self.assertIn("CANCEL", text)
        self.assertIn("2 minute", text)

    def test_confirm_returns_the_action_and_clears_it(self):
        self.mgr.request("shutdown", "Shut down?", {"delay": 30})
        claimed = self.mgr.confirm()
        self.assertEqual(claimed.action, "shutdown")
        self.assertEqual(claimed.params, {"delay": 30})
        self.assertIsNone(self.mgr.pending)

    def test_cancel_discards_the_action(self):
        self.mgr.request("shutdown", "Shut down?")
        self.assertIsNotNone(self.mgr.cancel())
        self.assertIsNone(self.mgr.pending)
        with self.assertRaises(ConfirmationError):
            self.mgr.confirm()

    def test_confirming_nothing_is_refused(self):
        with self.assertRaises(ConfirmationError) as ctx:
            self.mgr.confirm()
        self.assertIn("nothing waiting", str(ctx.exception))


class TestExpiry(unittest.TestCase):

    def setUp(self):
        self.mgr = ConfirmationManager()

    def test_expiry_is_two_minutes(self):
        self.assertEqual(EXPIRY_SECONDS, 120.0)

    def test_an_old_confirmation_is_not_a_standing_authorization(self):
        self.mgr.request("shutdown", "Shut down?")
        with mock.patch.object(confirm_mod.time, "time",
                               return_value=time.time() + EXPIRY_SECONDS + 1):
            with self.assertRaises(ConfirmationError) as ctx:
                self.mgr.confirm()
            self.assertIn("expired", str(ctx.exception))

    def test_expired_actions_disappear_from_pending(self):
        self.mgr.request("shutdown", "Shut down?")
        with mock.patch.object(confirm_mod.time, "time",
                               return_value=time.time() + EXPIRY_SECONDS + 1):
            self.assertIsNone(self.mgr.pending)

    def test_confirming_just_inside_the_window_works(self):
        self.mgr.request("shutdown", "Shut down?")
        with mock.patch.object(confirm_mod.time, "time",
                               return_value=time.time() + EXPIRY_SECONDS - 5):
            self.assertEqual(self.mgr.confirm().action, "shutdown")


class TestBinding(unittest.TestCase):
    """A confirmation belongs to one action, not to 'the last thing asked'."""

    def setUp(self):
        self.mgr = ConfirmationManager()

    def test_a_stale_token_cannot_confirm_a_newer_action(self):
        first = self.mgr.request("shutdown", "Shut down?")
        second = self.mgr.request("delete_file", "Move report.docx to the Bin?")
        self.assertNotEqual(first.token, second.token)
        with self.assertRaises(ConfirmationError):
            self.mgr.confirm(first.token)
        self.assertEqual(self.mgr.confirm(second.token).action, "delete_file")

    def test_a_wrong_token_does_not_discard_the_pending_action(self):
        pending = self.mgr.request("shutdown", "Shut down?")
        with self.assertRaises(ConfirmationError):
            self.mgr.confirm("zzzzzz")
        self.assertIs(self.mgr.pending, pending)

    def test_a_new_request_replaces_the_old_one(self):
        self.mgr.request("shutdown", "Shut down?")
        second = self.mgr.request("restart", "Restart?")
        self.assertIs(self.mgr.pending, second)

    def test_a_bare_confirm_is_accepted_because_only_one_can_be_pending(self):
        self.mgr.request("shutdown", "Shut down?")
        self.assertEqual(self.mgr.confirm(None).action, "shutdown")

    def test_token_matching_ignores_case(self):
        pending = self.mgr.request("shutdown", "Shut down?")
        self.assertTrue(self.mgr.confirm(pending.token.upper()))


class TestReplayProtection(unittest.TestCase):

    def setUp(self):
        self.mgr = ConfirmationManager()

    def test_a_confirmation_runs_at_most_once(self):
        pending = self.mgr.request("shutdown", "Shut down?")
        self.mgr.confirm(pending.token)
        with self.assertRaises(ConfirmationError):
            self.mgr.confirm(pending.token)

    def test_used_tokens_are_remembered(self):
        pending = self.mgr.request("shutdown", "Shut down?")
        self.mgr.confirm(pending.token)
        self.assertTrue(self.mgr.was_used(pending.token))

    def test_tokens_are_unpredictable(self):
        tokens = {ConfirmationManager().request("x", "y").token
                  for _ in range(300)}
        self.assertGreater(len(tokens), 290)

    def test_used_token_memory_does_not_grow_without_bound(self):
        for _ in range(600):
            self.mgr.confirm(self.mgr.request("x", "y").token)
        self.assertLessEqual(len(self.mgr._used_tokens), 501)


class TestPrivacy(unittest.TestCase):

    def test_repr_does_not_leak_parameters(self):
        pending = PendingAction(token="abc123", action="delete_file",
                                summary="Move a file to the Bin?",
                                params={"path": r"D:\private\diary.txt"},
                                created=time.time())
        for text in (repr(pending), str(pending)):
            self.assertNotIn("diary.txt", text)
            self.assertIn("delete_file", text)

    def test_the_manager_executes_nothing_itself(self):
        source = Path(confirm_mod.__file__).read_text(encoding="utf-8")
        for forbidden in ("subprocess", "Popen", "os.system", "os.remove",
                          "shutil", "eval(", "exec("):
            self.assertNotIn(forbidden, source, forbidden)


class TestThreadSafety(unittest.TestCase):

    def test_only_one_of_many_racing_confirms_wins(self):
        import threading
        mgr = ConfirmationManager()
        pending = mgr.request("shutdown", "Shut down?")
        wins = []

        def attempt():
            try:
                mgr.confirm(pending.token)
                wins.append(True)
            except ConfirmationError:
                pass

        threads = [threading.Thread(target=attempt) for _ in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        self.assertEqual(len(wins), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
