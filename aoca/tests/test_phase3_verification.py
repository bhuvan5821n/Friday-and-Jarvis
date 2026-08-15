"""Phase 3 tests: verification honesty, outcome storage, real launches.

The reality tests at the bottom launch actual processes. They are the point of
the phase: a verifier that only passes against mocks verifies mocks.
"""
from __future__ import annotations

import sys
import tempfile
import time
import unittest
from pathlib import Path

from aoca import verify as verification
from aoca.config import flags
from aoca.outcomes import OutcomeStore
from aoca.verify import (ExecutionResult, Outcome, ProcessIdentity,
                         VerificationResult, combine)

try:
    import psutil
    _PSUTIL = True
except ImportError:
    _PSUTIL = False


def _ok(**kwargs) -> ExecutionResult:
    return ExecutionResult(execution_started=True, tool="open_app", **kwargs)


class OutcomeRules(unittest.TestCase):
    def test_missing_verifier_is_unverified_not_succeeded(self):
        result = combine("open_app", _ok(),
                         VerificationResult.unavailable("none"), "Chrome")
        self.assertIs(result.outcome, Outcome.UNVERIFIED)
        self.assertFalse(result.succeeded)
        self.assertIn("couldn't confirm", result.message)

    def test_unverified_is_never_learnable(self):
        result = combine("open_app", _ok(),
                         VerificationResult.unavailable(), "Chrome")
        self.assertFalse(result.learnable)

    def test_started_but_not_observed_is_failure(self):
        result = combine("open_app", _ok(),
                         VerificationResult(True, False, "application_open",
                                            reason="no matching process"),
                         "Chrome")
        self.assertIs(result.outcome, Outcome.FAILED)
        self.assertIn("isn't running", result.message)
        self.assertTrue(result.learnable)

    def test_early_exit_is_its_own_outcome(self):
        result = combine("open_app", _ok(),
                         VerificationResult(True, False, "application_open",
                                            evidence={"early_exit": True}),
                         "Broken")
        self.assertIs(result.outcome, Outcome.STARTED_THEN_EXITED)
        self.assertNotIn("Opened", result.message)

    def test_success_needs_all_three(self):
        result = combine("open_app", _ok(),
                         VerificationResult(True, True, "application_open"),
                         "Chrome")
        self.assertIs(result.outcome, Outcome.SUCCEEDED)
        self.assertEqual(result.message, "Opened Chrome.")

    def test_not_started_is_failure_regardless_of_verification(self):
        execution = ExecutionResult(False, "open_app", error="not installed")
        result = combine("open_app", execution,
                         VerificationResult(True, True, "application_open"),
                         "Ghost")
        self.assertIs(result.outcome, Outcome.FAILED)

    def test_no_empty_result_becomes_an_invented_success(self):
        """Every non-SUCCEEDED message must avoid claiming it opened."""
        for verification_result in (
            VerificationResult.unavailable(),
            VerificationResult(True, False, "application_open"),
            VerificationResult(True, False, "application_open",
                               evidence={"early_exit": True}),
        ):
            result = combine("open_app", _ok(), verification_result, "Chrome")
            with self.subTest(outcome=result.outcome):
                self.assertNotIn("Opened Chrome", result.message)

    def test_event_payload_carries_no_text(self):
        result = combine("open_app", _ok(), VerificationResult(
            True, True, "application_open"), "Chrome")
        self.assertNotIn("message", result.as_event())


class VerifierRegistry(unittest.TestCase):
    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_VERIFICATION_ENABLED", True)

    def tearDown(self):
        flags.clear_overrides()

    def test_unknown_verifier_reports_unavailable(self):
        result = verification.verify("no_such_verifier", {})
        self.assertFalse(result.verification_completed)
        self.assertFalse(result.expected_state_observed)

    def test_disabled_flag_reports_unavailable_not_success(self):
        flags.set_override("AOCA_VERIFICATION_ENABLED", False)
        result = verification.verify("application_open", {})
        self.assertFalse(result.verification_completed)
        self.assertFalse(result.expected_state_observed)

    def test_no_registered_verifier_returns_true_unconditionally(self):
        """A fake verifier is worse than none, so assert none of them are."""
        for name in verification.verifier_names():
            with self.subTest(verifier=name):
                result = verification.verify(name, {})
                self.assertFalse(result.expected_state_observed,
                                 f"{name} claimed success on an empty context")

    def test_verifier_exception_does_not_become_success(self):
        verification.register_verifier(
            "explodes", lambda ctx: (_ for _ in ()).throw(RuntimeError("x")))
        try:
            result = verification.verify("explodes", {})
            self.assertFalse(result.verification_completed)
            self.assertFalse(result.expected_state_observed)
        finally:
            verification._VERIFIERS.pop("explodes", None)


class ProcessIdentityRules(unittest.TestCase):
    def test_key_includes_create_time_not_just_pid(self):
        first = ProcessIdentity(pid=1234, create_time=1000.0)
        second = ProcessIdentity(pid=1234, create_time=2000.0)
        self.assertNotEqual(first.key, second.key,
                            "a reused pid would impersonate the original")

    @unittest.skipUnless(_PSUTIL, "psutil not installed")
    def test_self_is_alive(self):
        proc = psutil.Process()
        identity = ProcessIdentity(pid=proc.pid, create_time=proc.create_time())
        self.assertTrue(identity.alive())

    @unittest.skipUnless(_PSUTIL, "psutil not installed")
    def test_wrong_create_time_is_not_alive(self):
        identity = ProcessIdentity(pid=psutil.Process().pid, create_time=1.0)
        self.assertFalse(identity.alive())


class FileVerifier(unittest.TestCase):
    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_VERIFICATION_ENABLED", True)

    def tearDown(self):
        flags.clear_overrides()

    def test_created_file_is_observed(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "made.txt"
            path.write_text("hi", encoding="utf-8")
            result = verification.verify("file_operation", {"path": str(path)})
            self.assertTrue(result.expected_state_observed)

    def test_missing_file_is_not_observed(self):
        result = verification.verify("file_operation",
                                     {"path": "/definitely/not/here.txt"})
        self.assertTrue(result.verification_completed)
        self.assertFalse(result.expected_state_observed)


class OutcomeStorage(unittest.TestCase):
    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_OUTCOME_STORAGE_ENABLED", True)
        self.folder = tempfile.TemporaryDirectory()
        self.store = OutcomeStore(Path(self.folder.name) / "test.db")

    def tearDown(self):
        self.store.close()
        self.folder.cleanup()
        flags.clear_overrides()

    def _record(self, result: VerificationResult) -> None:
        self.store.record(combine("open_app", _ok(), result, "Chrome"))

    def test_schema_migrates_on_first_use(self):
        self._record(VerificationResult(True, True, "application_open"))
        self.assertEqual(len(self.store.recent()), 1)

    def test_migration_is_idempotent(self):
        self._record(VerificationResult(True, True, "application_open"))
        self.store.close()
        second = OutcomeStore(self.store.path)
        try:
            self.assertEqual(len(second.recent()), 1)
        finally:
            second.close()

    def test_unverified_row_is_stored_but_not_learnable(self):
        self._record(VerificationResult.unavailable())
        row = self.store.recent()[0]
        self.assertEqual(row["outcome"], "unverified")
        self.assertEqual(row["learnable"], 0)

    def test_reliability_ignores_unverified_rows(self):
        self._record(VerificationResult(True, True, "application_open"))
        self._record(VerificationResult(True, False, "application_open"))
        self._record(VerificationResult.unavailable())
        stats = self.store.reliability("open_app")
        self.assertEqual(stats["verified"], 2)
        self.assertEqual(stats["succeeded"], 1)
        self.assertEqual(stats["rate"], 0.5)

    def test_no_rows_gives_no_rate_rather_than_zero(self):
        self.assertIsNone(self.store.reliability("never_run")["rate"])

    def test_storage_flag_off_writes_nothing(self):
        flags.set_override("AOCA_OUTCOME_STORAGE_ENABLED", False)
        self._record(VerificationResult(True, True, "application_open"))
        self.assertEqual(self.store.recent(), [])

    def test_write_failure_does_not_raise(self):
        self.store.close()
        self.store.path = Path(self.folder.name)   # a directory is not a db
        self._record(VerificationResult(True, True, "application_open"))
        self.assertEqual(self.store.stats.failed, 1)

    def test_stored_columns_carry_no_free_text(self):
        self._record(VerificationResult(True, True, "application_open"))
        row = self.store.recent()[0]
        self.assertEqual(set(row) - {
            "id", "trace_id", "span_id", "assistant", "origin", "tool",
            "method", "outcome", "execution_started", "verification_completed",
            "expected_state_observed", "verifier", "learnable", "duration_ms",
            "error_code", "occurred_at"}, set())


@unittest.skipUnless(_PSUTIL, "psutil not installed")
class RealityTests(unittest.TestCase):
    """Actual launches. These are what prove the verifier is not a mock."""

    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_VERIFICATION_ENABLED", True)

    def tearDown(self):
        flags.clear_overrides()

    def test_nonexistent_app_is_not_reported_as_opened(self):
        from actions import open_app as module

        # The real Start-Menu fallback types into the live desktop, so the
        # launcher is stubbed to the case under test: it reported a launch,
        # and nothing started.
        original = module._OS_LAUNCHERS.get(module._SYSTEM)
        module._OS_LAUNCHERS[module._SYSTEM] = lambda name: (True, "start_menu")
        try:
            message = module.open_app({"app_name": "zzqqxx_not_a_real_program"})
        finally:
            module._OS_LAUNCHERS[module._SYSTEM] = original
        self.assertNotIn("Opened", message)
        self.assertIn("isn't running", message)

    def test_known_app_that_really_starts_is_confirmed(self):
        """Launch a real, long-lived process and verify it is observed."""
        import subprocess

        stem = Path(sys.executable).stem
        before = verification.snapshot_processes(stem)
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(6)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            result = verification.verify("application_open", {
                "before": before, "expected_name": stem, "timeout": 8.0})
            self.assertTrue(result.verification_completed)
            self.assertTrue(result.expected_state_observed, result.reason)
            outcome = combine("open_app", _ok(), result, "python")
            self.assertIs(outcome.outcome, Outcome.SUCCEEDED)
            self.assertTrue(outcome.learnable)
        finally:
            proc.kill()
            proc.wait(timeout=5)

    def test_app_that_crashes_immediately_is_not_reported_as_opened(self):
        import subprocess

        stem = Path(sys.executable).stem
        before = verification.snapshot_processes(stem)
        # Lives long enough to be found by the poll, then dies before the
        # settle window ends — which is exactly the early-exit case.
        proc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(1); raise SystemExit(1)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            result = verification.verify("application_open", {
                "before": before, "expected_name": stem, "timeout": 8.0})
            outcome = combine("open_app", _ok(), result, "crasher")
            self.assertIsNot(outcome.outcome, Outcome.SUCCEEDED, result.reason)
            self.assertNotIn("Opened", outcome.message)
        finally:
            proc.wait(timeout=10)

    def test_stopped_process_is_verified_as_stopped(self):
        import subprocess

        proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                                stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
        identity = ProcessIdentity(
            pid=proc.pid, create_time=psutil.Process(proc.pid).create_time())
        self.assertTrue(identity.alive())
        proc.kill()
        proc.wait(timeout=5)
        result = verification.verify("process_stopped",
                                     {"identity": identity, "timeout": 5.0})
        self.assertTrue(result.expected_state_observed)

    def test_verification_completes_within_its_timeout(self):
        stem = "zzqq_absent_process"
        started = time.monotonic()
        verification.verify("application_open", {
            "before": {}, "expected_name": stem, "timeout": 1.0})
        self.assertLess(time.monotonic() - started, 6.0,
                        "verifier overran its timeout")


if __name__ == "__main__":
    unittest.main()
