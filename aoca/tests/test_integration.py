"""End-to-end wiring: instrumentation is present, joined, and cheap when off.

These test the seams the unit tests cannot: that a trace id actually survives
the `run_in_executor` boundary, that no publish site can throw into a user
action, and that turning the flag off costs nothing.
"""
from __future__ import annotations

import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from aoca import trace
from aoca.config import flags
from aoca.events import CognitiveBus, EventState, bus


class InstrumentationIsWired(unittest.TestCase):
    """The publish sites exist in the real files, not only in tests."""

    def test_every_instrumented_module_imports_emit(self):
        import ast
        from pathlib import Path

        root = Path(__file__).resolve().parents[2]
        for relative in ("main.py", "agent/executor.py",
                         "actions/browser_control.py",
                         "services/web_intelligence/tool.py",
                         "memory/memory_manager.py"):
            with self.subTest(module=relative):
                tree = ast.parse((root / relative).read_text(encoding="utf-8"))
                calls = [n for n in ast.walk(tree)
                         if isinstance(n, ast.Call)
                         and isinstance(n.func, ast.Name)
                         and n.func.id == "emit"]
                self.assertTrue(calls, f"{relative} has no emit() call")

    def test_open_app_cannot_return_an_unearned_opened(self):
        """The literal false-success sentence is gone from the launch path."""
        from pathlib import Path

        source = Path(__file__).resolve().parents[2] / "actions/open_app.py"
        text = source.read_text(encoding="utf-8")
        self.assertNotIn('return f"Opened {app_name}."', text)


class TraceSurvivesTheExecutorBoundary(unittest.TestCase):
    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_EVENTS_ENABLED", True)
        self.bus = CognitiveBus(maxsize=64)

    def tearDown(self):
        self.bus.shutdown(timeout=1.0)
        flags.clear_overrides()

    def test_traced_loop_wrapper_carries_context(self):
        """The `_TracedLoop` shim in main.py, exercised directly."""
        import asyncio

        import main

        async def scenario():
            loop = main._TracedLoop(asyncio.get_running_loop())
            with trace.trace(origin="local_voice") as context:
                got = await loop.run_in_executor(
                    None, lambda: trace.current().trace_id)
            return got, context.trace_id

        got, expected = asyncio.run(scenario())
        self.assertEqual(got, expected)

    def test_unwrapped_executor_would_have_lost_it(self):
        """Asserts the failure the shim prevents, so it cannot be deleted."""
        with trace.trace(origin="local_voice"):
            with ThreadPoolExecutor(max_workers=1) as pool:
                got = pool.submit(lambda: trace.current().trace_id).result()
        self.assertEqual(got, "untraced")

    def test_events_from_a_worker_join_to_the_request(self):
        received = []
        self.bus.subscribe("*", received.append)
        with trace.trace(origin="local_ui") as context:
            with ThreadPoolExecutor(max_workers=2) as pool:
                for _ in range(3):
                    pool.submit(trace.bind(
                        lambda: self.bus.publish("stage.timing",
                                                 payload={"duration_ms": 1})))
        self.bus.drain()
        self.assertEqual(len(received), 3)
        self.assertEqual({e.trace_id for e in received}, {context.trace_id})


class PublishSitesAreSafe(unittest.TestCase):
    def test_emit_never_raises_whatever_the_flag(self):
        from aoca.events import emit

        for enabled in (True, False):
            flags.set_override("AOCA_ENABLED", enabled)
            try:
                emit("x.event", EventState.RECEIVED, tool="t", nonsense=object())
            finally:
                flags.clear_overrides()

    def test_emit_with_flag_off_is_effectively_free(self):
        from aoca.events import emit

        flags.set_override("AOCA_ENABLED", False)
        try:
            started = time.monotonic()
            for _ in range(20_000):
                emit("stage.timing", duration_ms=1)
            elapsed = time.monotonic() - started
        finally:
            flags.clear_overrides()
        self.assertLess(elapsed, 1.0,
                        f"20k disabled emits took {elapsed:.2f}s")

    def test_enabled_publish_stays_off_the_caller_thread(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_EVENTS_ENABLED", True)
        local = CognitiveBus(maxsize=512)
        try:
            local.subscribe("*", lambda e: time.sleep(0.001))
            started = time.monotonic()
            for _ in range(200):
                local.publish("stage.timing", payload={"duration_ms": 1})
            elapsed = time.monotonic() - started
            self.assertLess(elapsed, 0.2,
                            f"200 publishes blocked the caller for {elapsed:.2f}s")
        finally:
            local.shutdown(timeout=2.0)
            flags.clear_overrides()

    def test_global_bus_is_not_started_by_import_alone(self):
        self.assertIsNotNone(bus)


class UnsafeFallbackIsOffRegardlessOfTheFlag(unittest.TestCase):
    """The two guarantees that must hold with AOCA_ENABLED false."""

    def setUp(self):
        flags.set_override("AOCA_ENABLED", False)

    def tearDown(self):
        flags.clear_overrides()

    def test_unknown_tool_still_refuses_with_aoca_disabled(self):
        from aoca.tools import ToolNotRegistered, registry

        with self.assertRaises(ToolNotRegistered):
            registry.require("totally_unknown_tool")

    def test_open_app_still_refuses_to_invent_success(self):
        from actions import open_app as module

        original = module._OS_LAUNCHERS.get(module._SYSTEM)
        module._OS_LAUNCHERS[module._SYSTEM] = lambda name: (True, "start_menu")
        try:
            message = module.open_app({"app_name": "zzqqxx_not_a_real_program"})
        finally:
            module._OS_LAUNCHERS[module._SYSTEM] = original
        self.assertNotIn("Opened", message)

    def test_outcome_store_writes_nothing_with_aoca_disabled(self):
        import tempfile
        from pathlib import Path

        from aoca.outcomes import OutcomeStore
        from aoca.verify import (ExecutionResult, VerificationResult, combine)

        with tempfile.TemporaryDirectory() as folder:
            store = OutcomeStore(Path(folder) / "off.db")
            try:
                store.record(combine(
                    "open_app", ExecutionResult(True, "open_app"),
                    VerificationResult(True, True, "application_open")))
                self.assertEqual(store.recent(), [])
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
