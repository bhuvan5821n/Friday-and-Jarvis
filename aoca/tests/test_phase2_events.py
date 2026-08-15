"""Phase 2 tests: trace propagation, privacy filtering, bus behaviour."""
from __future__ import annotations

import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aoca import trace
from aoca.config import flags
from aoca.events import (CognitiveBus, CognitiveEvent, EventState, Priority,
                         StateMachine)
from aoca.privacy import ALLOWED_FIELDS, DENIED_FIELDS, redact, sanitize


class TraceContextPropagation(unittest.TestCase):
    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_EVENTS_ENABLED", True)

    def tearDown(self):
        flags.clear_overrides()

    def test_no_module_level_mutable_global(self):
        text = Path(trace.__file__).read_text(encoding="utf-8")
        self.assertIn("contextvars", text)
        # A bare module-level dict/list holding request state is the failure
        # mode this replaces.
        self.assertNotIn("\n_state = {}", text)

    def test_untraced_by_default(self):
        self.assertFalse(trace.is_traced())
        self.assertEqual(trace.current().trace_id, "untraced")

    def test_trace_id_is_stable_within_a_request(self):
        with trace.trace(origin="local_ui") as context:
            first = trace.current().trace_id
            with trace.stage("routing"):
                self.assertEqual(trace.current().trace_id, first)
                self.assertEqual(trace.current().stage, "routing")
            self.assertEqual(trace.current().stage, "request")
        self.assertEqual(context.trace_id, first)

    def test_child_span_links_to_parent(self):
        with trace.trace():
            root = trace.current()
            with trace.stage("execute"):
                child = trace.current()
        self.assertEqual(child.parent_span, root.span_id)
        self.assertNotEqual(child.span_id, root.span_id)

    def test_concurrent_requests_do_not_share_a_trace_id(self):
        seen: list[str] = []
        barrier = threading.Barrier(4)

        def request(index: int) -> str:
            with trace.trace(origin=f"origin{index}"):
                barrier.wait(timeout=5)
                time.sleep(0.01)
                return trace.current().trace_id

        with ThreadPoolExecutor(max_workers=4) as pool:
            seen = [f.result() for f in
                    [pool.submit(request, i) for i in range(4)]]
        self.assertEqual(len(set(seen)), 4, "trace ids collided across threads")

    def test_context_does_not_leak_into_unbound_thread(self):
        """The failure `bind` exists to prevent, asserted rather than assumed."""
        captured: list[str] = []

        def worker():
            captured.append(trace.current().trace_id)

        with trace.trace(origin="local_ui"):
            thread = threading.Thread(target=worker)
            thread.start()
            thread.join()
        self.assertEqual(captured, ["untraced"])

    def test_bind_carries_context_across_threads(self):
        captured: list[str] = []

        def worker():
            captured.append(trace.current().trace_id)

        with trace.trace(origin="local_ui") as context:
            thread = threading.Thread(target=trace.bind(worker))
            thread.start()
            thread.join()
        self.assertEqual(captured, [context.trace_id])

    def test_bind_works_with_thread_pool(self):
        with trace.trace() as context:
            with ThreadPoolExecutor(max_workers=2) as pool:
                got = pool.submit(
                    trace.bind(lambda: trace.current().trace_id)).result()
        self.assertEqual(got, context.trace_id)

    def test_deadline_and_cancellation_are_visible_to_stages(self):
        cancel = threading.Event()
        with trace.trace(timeout=0.05, cancel=cancel):
            self.assertFalse(trace.current().expired)
            self.assertFalse(trace.current().cancelled)
            cancel.set()
            self.assertTrue(trace.current().cancelled)
            time.sleep(0.06)
            self.assertTrue(trace.current().expired)
            self.assertEqual(trace.current().remaining(), 0.0)

    def test_child_cannot_extend_parent_deadline(self):
        with trace.trace(timeout=10.0) as parent:
            with trace.stage("inner"):
                self.assertEqual(trace.current().deadline, parent.deadline)

    def test_fields_carry_no_request_text(self):
        with trace.trace(origin="local_voice"):
            fields = trace.current().as_fields()
        self.assertEqual(set(fields), {"trace_id", "assistant", "origin",
                                       "stage", "span_id", "parent_span"})


class PrivacyFilter(unittest.TestCase):
    def test_denied_fields_are_dropped(self):
        payload = {name: "sensitive" for name in DENIED_FIELDS}
        clean = sanitize(payload)
        for name in DENIED_FIELDS:
            self.assertNotIn(name, clean, name)

    def test_unknown_fields_are_dropped_without_a_pattern(self):
        clean = sanitize({"totally_new_field": "whatever",
                          "banking_details": "1234", "tool": "open_app"})
        self.assertEqual(set(clean) - {"dropped_fields"}, {"tool"})
        self.assertEqual(clean["dropped_fields"], 2)

    def test_allowlisted_fields_survive(self):
        payload = {"tool": "open_app", "duration_ms": 42, "permitted": True}
        self.assertEqual(sanitize(payload), payload)

    def test_secrets_inside_allowed_fields_are_redacted(self):
        for secret in ["sk-abcdefghijklmnopqrstuvwx",
                       "AIzaSyABCDEFGHIJKLMNOPQRSTUVWXYZ0123456",
                       "ghp_abcdefghijklmnopqrstuvwxyz01",
                       "Bearer abcdefghijklmnopqrst",
                       "a" * 40,
                       "password=hunter2"]:
                with self.subTest(secret=secret):
                    clean = sanitize({"message": f"failed with {secret}"})
                    self.assertNotIn(secret, clean["message"])

    def test_otp_and_private_key_are_redacted(self):
        clean = sanitize({"message": "OTP: 483920"})
        self.assertNotIn("483920", clean["message"])
        key = ("-----BEGIN RSA PRIVATE KEY-----\nMIIEowIB\n"
               "-----END RSA PRIVATE KEY-----")
        self.assertNotIn("MIIEowIB", sanitize({"message": key})["message"])

    def test_long_values_are_clamped(self):
        clean = sanitize({"message": "x" * 5000})
        self.assertLess(len(clean["message"]), 500)

    def test_nested_dicts_are_filtered_too(self):
        clean = sanitize({"outcome": {"tool": "open_app",
                                      "password": "hunter2"}})
        self.assertNotIn("password", clean["outcome"])

    def test_filter_never_raises(self):
        class Hostile:
            def __repr__(self):
                raise RuntimeError("boom")

        for payload in (None, [], "string", 5, {"tool": Hostile()}):
            sanitize(payload)   # must not raise

    def test_allow_and_deny_lists_do_not_overlap(self):
        self.assertEqual(ALLOWED_FIELDS & DENIED_FIELDS, frozenset())

    def test_redact_handles_none(self):
        self.assertEqual(redact(None), "")


class BusBehaviour(unittest.TestCase):
    def setUp(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_EVENTS_ENABLED", True)
        self.bus = CognitiveBus(maxsize=8)

    def tearDown(self):
        self.bus.shutdown(timeout=1.0)
        flags.clear_overrides()

    def test_publish_delivers_to_subscriber(self):
        received: list[CognitiveEvent] = []
        self.bus.subscribe("test.event", received.append)
        with trace.trace(origin="local_ui"):
            self.bus.publish("test.event", payload={"tool": "open_app"})
        self.assertTrue(self.bus.drain())
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0].payload["tool"], "open_app")

    def test_wildcard_subscriber_sees_everything(self):
        received: list[CognitiveEvent] = []
        self.bus.subscribe("*", received.append)
        self.bus.publish("a.one")
        self.bus.publish("b.two")
        self.bus.drain()
        self.assertEqual(len(received), 2)

    def test_publish_does_not_block_on_a_slow_subscriber(self):
        self.bus.subscribe("slow.event", lambda e: time.sleep(0.3))
        start = time.monotonic()
        for _ in range(5):
            self.bus.publish("slow.event")
        self.assertLess(time.monotonic() - start, 0.1,
                        "publish blocked on the subscriber")

    def test_handler_exception_does_not_stop_other_handlers(self):
        received = []
        self.bus.subscribe("x.event", lambda e: (_ for _ in ()).throw(
            RuntimeError("bad handler")))
        self.bus.subscribe("x.event", received.append)
        self.bus.publish("x.event")
        self.bus.drain()
        self.assertEqual(len(received), 1)
        self.assertEqual(self.bus.stats.handler_errors, 1)

    def test_handler_exception_does_not_kill_the_dispatcher(self):
        self.bus.subscribe("y.event", lambda e: (_ for _ in ()).throw(
            RuntimeError("bad")))
        received = []
        self.bus.subscribe("z.event", received.append)
        self.bus.publish("y.event")
        self.bus.drain()
        self.bus.publish("z.event")
        self.bus.drain()
        self.assertEqual(len(received), 1)

    def test_overload_drops_telemetry_and_keeps_consequences(self):
        blocked = threading.Event()
        self.bus.subscribe("*", lambda e: blocked.wait(timeout=3))
        for i in range(60):
            self.bus.publish("stage.timing", payload={"duration_ms": i})
        kept = self.bus.publish("policy.denied", payload={"tool": "x"})
        blocked.set()
        self.bus.drain(timeout=3)
        self.assertTrue(kept, "a consequence event was dropped under overload")
        self.assertGreater(self.bus.stats.dropped, 0,
                           "telemetry was not dropped under overload")

    def test_queue_is_bounded(self):
        blocked = threading.Event()
        self.bus.subscribe("*", lambda e: blocked.wait(timeout=3))
        for _ in range(500):
            self.bus.publish("stage.timing")
        self.assertLessEqual(self.bus.stats.max_queue_depth, 8)
        blocked.set()

    def test_payload_is_sanitized_at_construction(self):
        received: list[CognitiveEvent] = []
        self.bus.subscribe("s.event", received.append)
        self.bus.publish("s.event", payload={"password": "hunter2",
                                             "tool": "open_app"})
        self.bus.drain()
        self.assertNotIn("password", received[0].payload)
        self.assertNotIn("hunter2", str(received[0].as_dict()))

    def test_events_carry_the_current_trace(self):
        received: list[CognitiveEvent] = []
        self.bus.subscribe("t.event", received.append)
        with trace.trace(origin="local_voice",
                         assistant=trace.Assistant.FRIDAY) as context:
            self.bus.publish("t.event")
        self.bus.drain()
        self.assertEqual(received[0].trace_id, context.trace_id)
        self.assertEqual(received[0].assistant, "FRIDAY")
        self.assertEqual(received[0].origin, "local_voice")

    def test_disabled_flag_stops_publishing(self):
        received = []
        self.bus.subscribe("d.event", received.append)
        flags.set_override("AOCA_EVENTS_ENABLED", False)
        self.assertFalse(self.bus.publish("d.event"))
        self.bus.drain()
        self.assertEqual(received, [])

    def test_master_flag_off_stops_publishing(self):
        received = []
        self.bus.subscribe("m.event", received.append)
        flags.set_override("AOCA_ENABLED", False)
        self.assertFalse(self.bus.publish("m.event"))
        self.bus.drain()
        self.assertEqual(received, [])

    def test_unsubscribe_stops_delivery(self):
        received = []
        cancel = self.bus.subscribe("u.event", received.append)
        cancel()
        self.bus.publish("u.event")
        self.bus.drain()
        self.assertEqual(received, [])

    def test_shutdown_delivers_pending_events(self):
        received = []
        self.bus.subscribe("f.event", received.append)
        for _ in range(5):
            self.bus.publish("f.event")
        self.bus.shutdown(timeout=2.0)
        self.assertEqual(len(received), 5)

    def test_priority_classification(self):
        cases = [
            ("policy.denied", None, Priority.CONSEQUENCE),
            ("security.refused", None, Priority.CONSEQUENCE),
            ("action.outcome", None, Priority.CONSEQUENCE),
            ("stage.timing", None, Priority.TELEMETRY),
            ("request.state", EventState.ROUTED, Priority.LIFECYCLE),
            ("request.state", EventState.FAILED, Priority.CONSEQUENCE),
        ]
        for event_type, state, expected in cases:
            with self.subTest(event_type=event_type, state=state):
                event = CognitiveEvent.create(event_type, state)
                self.assertEqual(event.priority, expected)


class StateMachineRules(unittest.TestCase):
    def test_legal_happy_path(self):
        machine = StateMachine()
        path = [EventState.RECEIVED, EventState.ROUTED,
                EventState.POLICY_CHECKED, EventState.EXECUTING,
                EventState.EXECUTED, EventState.VERIFYING,
                EventState.COMPLETED]
        for state in path:
            self.assertTrue(machine.advance("t1", state), state)

    def test_cannot_skip_to_executing(self):
        machine = StateMachine()
        machine.advance("t2", EventState.RECEIVED)
        self.assertFalse(machine.advance("t2", EventState.EXECUTING))

    def test_terminal_state_is_terminal(self):
        machine = StateMachine()
        machine.advance("t3", EventState.RECEIVED)
        self.assertTrue(machine.advance("t3", EventState.REFUSED))
        for state in EventState:
            self.assertFalse(machine.advance("t3", state), state)

    def test_executed_may_go_unverified_not_completed_silently(self):
        machine = StateMachine()
        for state in (EventState.RECEIVED, EventState.ROUTED,
                      EventState.POLICY_CHECKED, EventState.EXECUTING,
                      EventState.EXECUTED):
            machine.advance("t4", state)
        self.assertTrue(machine.advance("t4", EventState.UNVERIFIED))

    def test_illegal_transition_still_publishes_the_event(self):
        flags.set_override("AOCA_ENABLED", True)
        flags.set_override("AOCA_EVENTS_ENABLED", True)
        bus = CognitiveBus(maxsize=8)
        try:
            received = []
            bus.subscribe("*", received.append)
            with trace.trace():
                bus.publish("request.state", EventState.EXECUTING)
            bus.drain()
            self.assertEqual(len(received), 1, "event lost on illegal state")
            self.assertEqual(bus.stats.illegal_transitions, 1)
        finally:
            bus.shutdown(timeout=1.0)
            flags.clear_overrides()

    def test_tracked_traces_are_bounded(self):
        machine = StateMachine()
        for i in range(1000):
            machine.advance(f"trace{i}", EventState.RECEIVED)
        self.assertLessEqual(len(machine._states), machine._MAX_TRACES + 1)


if __name__ == "__main__":
    unittest.main()
