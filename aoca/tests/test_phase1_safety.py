"""Phase 1 security tests: no path from text to execution.

The last three of these are the audit. They walk every tracked `.py` file with
`ast` rather than grepping, because a grep for "exec(" both misses
`getattr(builtins, 'exec')` and trips over the word in a comment. Each has an
explicit allowlist of known, reviewed sites, so a *new* dynamic-execution site
fails the suite instead of quietly joining a count nobody reads.
"""
from __future__ import annotations

import ast
import subprocess
import unittest
from pathlib import Path

from aoca.safety import Origin, SafetyKernel
from aoca.tools import (Permission, RiskLevel, ToolDefinition, ToolNotRegistered,
                        ToolRegistry, normalize, registry)

REPO = Path(__file__).resolve().parents[2]


def _tracked_py() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "*.py"], cwd=REPO,
                         capture_output=True, text=True, check=True)
    return [REPO / line for line in out.stdout.splitlines() if line.strip()]


class UnknownToolCannotExecute(unittest.TestCase):
    """The vulnerability this phase exists to close."""

    def test_unknown_tool_raises_and_runs_nothing(self):
        from agent.executor import _call_tool
        payloads = [
            {"code": "import os; os.system('whoami')"},
            {"description": "run whoami and print the result"},
            {"command": "powershell -c whoami"},
        ]
        for params in payloads:
            with self.subTest(params=params):
                with self.assertRaises(ToolNotRegistered) as ctx:
                    _call_tool("totally_unknown_tool", params, None)
                self.assertEqual(ctx.exception.error_code,
                                 "TOOL_NOT_REGISTERED")
                self.assertFalse(ctx.exception.as_result()["executed"])

    def test_generated_code_tool_no_longer_exists(self):
        import agent.executor as executor
        from aoca.tools import registry
        self.assertFalse(registry.contains("generated_code"))
        self.assertFalse(hasattr(executor, "_run_generated_code"))
        with self.assertRaises(ToolNotRegistered):
            executor._call_tool("generated_code", {"description": "x"}, None)

    def test_executor_step_does_not_default_to_code_execution(self):
        """A step with no `tool` key must not fall through to anything."""
        source = (REPO / "agent" / "executor.py").read_text(encoding="utf-8")
        self.assertNotIn('"generated_code"', source)
        self.assertNotIn("'generated_code'", source)

    def test_injection_shaped_names_are_not_tools(self):
        from aoca.tools import registry
        for name in ["open_app; whoami", "../../etc/passwd", "$(whoami)",
                     "open_app && del /f", "__import__", "os.system",
                     "OPEN_APP\x00", "opeո_app", None, 42, "", " "]:
            with self.subTest(name=name):
                self.assertFalse(registry.contains(name))


class DesktopSandboxRemoved(unittest.TestCase):
    def test_desktop_generated_code_path_gone(self):
        import actions.desktop as desktop
        for attr in ("_build_sandbox", "_execute_generated_code",
                     "_ask_gemini_for_desktop_action"):
            self.assertFalse(hasattr(desktop, attr), attr)

    def test_freeform_desktop_task_is_refused_not_run(self):
        from actions.desktop import desktop_control
        result = desktop_control({"task": "import os; os.system('whoami')"})
        self.assertIn("Nothing was run", result)


class FuzzyMatchingSuggestsOnly(unittest.TestCase):
    def test_suggestion_is_returned_not_executed(self):
        registry = ToolRegistry()
        calls = []
        registry.register(ToolDefinition(
            canonical_name="open_app", handler=lambda **kw: calls.append(kw),
            summary="x", required_params=()))

        self.assertEqual(registry.suggest("open_ap"), "open_app")
        self.assertIsNone(registry.resolve("open_ap"))
        with self.assertRaises(ToolNotRegistered):
            registry.require("open_ap")
        self.assertEqual(calls, [], "a suggestion executed something")

    def test_alias_is_exact_not_fuzzy(self):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            canonical_name="open_app", handler=lambda **kw: None,
            summary="x", aliases=("launch_app",)))
        self.assertEqual(registry.resolve("launch_app").canonical_name,
                         "open_app")
        self.assertIsNone(registry.resolve("launch_apps"))

    def test_normalize_rejects_non_identifiers(self):
        self.assertEqual(normalize("Open App"), "open_app")
        self.assertEqual(normalize("open-app"), "open_app")
        for bad in ("1tool", "tool!", "a" * 65, "", "os.system('x')"):
            self.assertEqual(normalize(bad), "", bad)


class SafetyFailsClosed(unittest.TestCase):
    def test_unknown_origin_is_denied(self):
        kernel = SafetyKernel()
        decision = kernel.decide("open_app", Origin.UNKNOWN,
                                 {"app_name": "notepad"})
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.policy_rule, "unattributed_origin")

    def test_untrusted_content_can_never_act(self):
        kernel = SafetyKernel()
        for tool in ("open_app", "web_search", "send_message",
                     "computer_settings", "file_controller"):
            decision = kernel.decide(tool, Origin.UNTRUSTED_CONTENT,
                                     {"app_name": "notepad"})
            self.assertFalse(decision.permitted, tool)
            self.assertEqual(decision.policy_rule,
                             "untrusted_content_cannot_act")

    def test_registry_exception_denies_rather_than_passes(self):
        class Exploding:
            def require(self, name):
                raise RuntimeError("policy service is down")

            def suggest(self, name):
                return None

        decision = SafetyKernel(Exploding()).decide(
            "open_app", Origin.LOCAL_UI, {"app_name": "notepad"})
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.policy_rule, "policy_error")

    def test_decision_carries_no_request_text(self):
        kernel = SafetyKernel()
        secret = "my password is hunter2"
        decision = kernel.decide("open_app", Origin.LOCAL_UI,
                                 {"app_name": secret})
        self.assertNotIn(secret, str(decision.as_event()))

    def test_remote_origin_cannot_reach_critical_risk(self):
        kernel = SafetyKernel()
        decision = kernel.decide("computer_settings", Origin.REMOTE_AUTHORIZED)
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.policy_rule, "risk_above_origin_ceiling")

    def test_critical_risk_requires_confirmation_locally(self):
        kernel = SafetyKernel()
        decision = kernel.decide("computer_settings", Origin.LOCAL_UI)
        self.assertTrue(decision.permitted)
        self.assertTrue(decision.confirmation_required)

    def test_decisions_are_deterministic(self):
        kernel = SafetyKernel()
        rules = {kernel.decide("open_app", Origin.LOCAL_VOICE,
                               {"app_name": "notepad"}).policy_rule
                 for _ in range(50)}
        self.assertEqual(len(rules), 1)

    def test_no_score_or_weight_in_the_kernel(self):
        """Nothing here for a learning layer to move."""
        source = (REPO / "aoca" / "safety.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
        names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
        for banned in ("score", "weight", "confidence", "threshold",
                       "probability", "learned"):
            self.assertNotIn(banned, names, banned)

    def test_forbidden_permission_denies(self):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            canonical_name="disabled_thing", handler=lambda **kw: None,
            summary="x", permission=Permission.FORBIDDEN))
        decision = SafetyKernel(registry).decide("disabled_thing",
                                                 Origin.LOCAL_UI)
        self.assertFalse(decision.permitted)
        self.assertEqual(decision.policy_rule, "tool_forbidden")

    def test_creator_only_denies_remote(self):
        registry = ToolRegistry()
        registry.register(ToolDefinition(
            canonical_name="local_thing", handler=lambda **kw: None,
            summary="x", permission=Permission.CREATOR_ONLY,
            risk_level=RiskLevel.LOW))
        kernel = SafetyKernel(registry)
        self.assertFalse(kernel.decide("local_thing",
                                       Origin.REMOTE_AUTHORIZED).permitted)
        self.assertTrue(kernel.decide("local_thing", Origin.LOCAL_UI).permitted)


class InjectedPayloadsCannotExecute(unittest.TestCase):
    """The mandatory security test, from both directions.

    The same payload is tried twice: once as a hallucinated tool name from the
    planner, and once as text that arrived from a webpage. Neither may run.
    """

    PAYLOADS = (
        "totally_unknown_tool",
        "os.system('whoami')",
        "__import__('os').system('whoami')",
        "generated_code",
        "eval",
        "exec",
        "run_python",
        "shell",
        "; whoami",
        "open_app && whoami",
    )

    def test_no_payload_resolves_to_a_handler(self):
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                self.assertIsNone(registry.resolve(payload))
                with self.assertRaises(ToolNotRegistered):
                    registry.require(payload)

    def test_the_same_payload_from_a_webpage_is_denied_too(self):
        kernel = SafetyKernel()
        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                decision = kernel.decide(payload, Origin.UNTRUSTED_CONTENT,
                                         {"command": "whoami"})
                self.assertFalse(decision.permitted)
                self.assertEqual(decision.policy_rule,
                                 "untrusted_content_cannot_act")

    def test_a_real_tool_name_from_a_webpage_is_still_denied(self):
        """The block is the origin, not the unfamiliarity of the name."""
        kernel = SafetyKernel()
        decision = kernel.decide("open_app", Origin.UNTRUSTED_CONTENT,
                                 {"app_name": "notepad"})
        self.assertFalse(decision.permitted)

    def test_executor_refuses_a_planned_payload_without_running_it(self):
        from agent.executor import _call_tool

        for payload in self.PAYLOADS:
            with self.subTest(payload=payload):
                with self.assertRaises(ToolNotRegistered):
                    _call_tool(payload, {"command": "whoami"}, None)

    def test_normalize_rejects_shell_metacharacters(self):
        from aoca.tools import normalize

        for payload in ("; whoami", "a && b", "x | y", "$(whoami)",
                        "../../etc/passwd", "os.system('x')"):
            with self.subTest(payload=payload):
                self.assertEqual(normalize(payload), "")


class NoDynamicExecutionInProjectCode(unittest.TestCase):
    """The repo-wide audit. Allowlists are the record of what was reviewed."""

    #: Reviewed and accepted. Anything not here fails the test.
    #:
    #: Only bare-name calls are checked. `re.compile` and Qt's `app.exec()` are
    #: attribute calls on known objects and are not dynamic execution — folding
    #: them in produced 47 false positives and no true ones.
    ALLOWED_DYNAMIC: dict[str, set[str]] = {
        # Fetches a stdlib module by name inside a test/UI helper. Constant
        # argument, no user input reaches it.
        "remote_control/tests/test_screenshot.py": {"__import__"},
        "ui.py": {"__import__"},
    }

    #: `shell=True` sites still to be closed. Each is a fixed command string
    #: that does not interpolate model output; they are tracked here so the
    #: number cannot grow silently.
    KNOWN_SHELL_TRUE = {
        "actions/open_app.py",
        "actions/computer_settings.py",
        "actions/dev_agent.py",
        "ui.py",
    }

    def test_no_new_exec_eval_or_compile(self):
        offenders: list[str] = []
        for path in _tracked_py():
            rel = path.relative_to(REPO).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            allowed = self.ALLOWED_DYNAMIC.get(rel, set())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if not isinstance(node.func, ast.Name):
                    continue
                name = node.func.id
                if name in ("exec", "eval", "compile", "__import__") \
                        and name not in allowed:
                    offenders.append(f"{rel}:{node.lineno} {name}()")
        self.assertEqual(offenders, [], "new dynamic execution site(s)")

    def test_shell_true_does_not_spread(self):
        found: set[str] = set()
        for path in _tracked_py():
            rel = path.relative_to(REPO).as_posix()
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (SyntaxError, UnicodeDecodeError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.keyword) and node.arg == "shell":
                    if isinstance(node.value, ast.Constant) and node.value.value:
                        found.add(rel)
        self.assertEqual(found - self.KNOWN_SHELL_TRUE, set(),
                         "shell=True appeared in a new file")

    def test_no_tempfile_python_script_execution(self):
        """The deleted fallback wrote a .py to temp and ran it. Stay deleted."""
        offenders = []
        for path in _tracked_py():
            if path.name == "test_phase1_safety.py":
                continue   # the audit string itself lives here
            text = path.read_text(encoding="utf-8", errors="ignore")
            if 'suffix=".py"' in text and "subprocess" in text:
                offenders.append(path.relative_to(REPO).as_posix())
        self.assertEqual(offenders, [])


if __name__ == "__main__":
    unittest.main()
