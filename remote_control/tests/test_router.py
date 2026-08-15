"""Phase 4 tests: routing to JARVIS, FRIDAY, and NEXUS.

The model is mocked throughout. These assert on *routing and identity* — which
persona was addressed, which prompt it was given, and which answers must never
depend on a model at all. Whether the model writes good prose is not testable
here and is not what this phase is for.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control.bridge_protocol import Request
from remote_control.router import MAX_COMMAND_CHARS, route


def setUpModule():
    """Routing now writes an audit record. Send this module's records to a
    temporary file so running the tests never pollutes the real remote log."""
    import tempfile
    from remote_control import audit

    global _audit_dir, _audit_patches
    _audit_dir = tempfile.TemporaryDirectory()
    path = Path(_audit_dir.name) / "audit.jsonl"
    _audit_patches = [mock.patch.object(audit, "AUDIT_PATH", path),
                      mock.patch.object(audit, "_RUNTIME", path.parent)]
    for patch in _audit_patches:
        patch.start()


def tearDownModule():
    for patch in _audit_patches:
        patch.stop()
    _audit_dir.cleanup()


def _ask(target, command):
    return route(Request(action="ASK", target=target, command=command))


class TestCreatorIdentity(unittest.TestCase):
    """Answered locally, before any model, for both assistants."""

    QUESTIONS = ["who created you", "who made you", "Who built you?",
                 "whose creation are you", "who is your creator",
                 "tumhe kisne banaya", "who developed you",
                 "who designed you", "who programmed you"]

    def test_both_assistants_credit_bhuvan(self):
        for target in ("jarvis", "friday"):
            for question in self.QUESTIONS:
                with mock.patch("core.ai.ask",
                                side_effect=AssertionError("model was called")):
                    reply = _ask(target, question)
                self.assertTrue(reply.ok, question)
                self.assertIn("Bhuvan", reply.text, f"{target}: {question}")

    def test_no_company_or_model_is_ever_credited(self):
        for target in ("jarvis", "friday"):
            for question in self.QUESTIONS:
                with mock.patch("core.ai.ask", side_effect=AssertionError):
                    text = _ask(target, question).text.lower()
                for forbidden in ("openai", "anthropic", "google", "gemini",
                                  "claude", "a developer", "development team",
                                  "language model", "unknown"):
                    self.assertNotIn(forbidden, text, f"{question}: {forbidden}")

    def test_it_works_with_no_model_reachable(self):
        with mock.patch("core.ai.ask", side_effect=OSError("offline")):
            reply = _ask("friday", "who created you")
        self.assertTrue(reply.ok)
        self.assertIn("Bhuvan", reply.text)

    def test_foundation_model_questions_stay_truthful(self):
        """Bhuvan created the assistants, not the underlying models."""
        with mock.patch("core.ai.ask", return_value="Anthropic created Claude.") as m:
            reply = _ask("jarvis", "who created the Claude model")
        m.assert_called_once()                       # routed to the model
        self.assertNotIn("Bhuvan created me", reply.text)


class TestPersonaSeparation(unittest.TestCase):
    """JARVIS and FRIDAY must stay two assistants, not one."""

    def _prompt_for(self, target):
        with mock.patch("core.ai.ask", return_value="ok") as m:
            _ask(target, "explain quicksort")
        return m.call_args.kwargs["system"]

    def test_each_persona_gets_a_different_system_prompt(self):
        self.assertNotEqual(self._prompt_for("jarvis"),
                            self._prompt_for("friday"))

    def test_friday_keeps_her_own_character(self):
        friday = self._prompt_for("friday")
        self.assertIn("FRIDAY", friday)
        self.assertNotIn("FRIDAY", self._prompt_for("jarvis"))

    def test_the_persona_prompts_come_from_the_app_not_a_copy(self):
        """A change to either persona must reach WhatsApp automatically."""
        import main
        self.assertIn(main.PERSONAS["friday"]["prompt"].strip()[:60],
                      self._prompt_for("friday"))

    def test_remote_replies_are_text_only(self):
        for target in ("jarvis", "friday"):
            prompt = self._prompt_for(target)
            self.assertIn("WhatsApp", prompt)
            self.assertIn("no tool calls", prompt)


class TestNexusIsNotAnAssistant(unittest.TestCase):

    def test_nexus_never_reaches_a_model(self):
        with mock.patch("core.ai.ask",
                        side_effect=AssertionError("NEXUS used a model")):
            reply = _ask("nexus", "write me a poem about the sea")
        self.assertTrue(reply.ok)
        self.assertIn("Jarvis", reply.text)

    def test_nexus_describes_itself_as_a_gateway(self):
        with mock.patch("core.ai.ask", side_effect=AssertionError):
            text = _ask("nexus", "what are you").text
        self.assertIn("gateway", text.lower())
        self.assertIn("not a separate assistant", text.lower())

    def test_nexus_still_answers_status(self):
        with mock.patch("core.ai.ask", side_effect=AssertionError):
            reply = _ask("nexus", "status")
        self.assertTrue(reply.ok)
        self.assertIn("CPU:", reply.text)


class TestStatusShortcuts(unittest.TestCase):
    """Status must not cost a model call from any persona."""

    def test_status_words_answer_locally(self):
        for command in ("status", "battery level", "how much RAM is free",
                        "is the laptop online", "what is the temperature"):
            with mock.patch("core.ai.ask", side_effect=AssertionError(command)):
                reply = _ask("jarvis", command)
            self.assertTrue(reply.ok, command)
            self.assertEqual(reply.data["source"], "local")

    def test_whats_happening_answers_locally(self):
        with mock.patch("core.ai.ask", side_effect=AssertionError):
            reply = _ask("friday", "what are you doing right now")
        self.assertTrue(reply.ok)


class TestFailureHandling(unittest.TestCase):

    def test_unreachable_model_is_admitted_not_faked(self):
        with mock.patch("core.ai.ask", side_effect=OSError("no route")):
            reply = _ask("jarvis", "explain quicksort")
        self.assertFalse(reply.ok)
        self.assertIn("not reachable", reply.error)
        self.assertIn("JARVIS", reply.error)

    def test_model_internals_are_not_leaked_to_whatsapp(self):
        with mock.patch("core.ai.ask", side_effect=OSError("api_key=sk-secret")):
            reply = _ask("jarvis", "hello")
        self.assertNotIn("sk-secret", reply.error)

    def test_empty_answer_is_reported(self):
        with mock.patch("core.ai.ask", return_value="   "):
            self.assertFalse(_ask("friday", "hello").ok)

    def test_empty_command_gets_a_prompt_not_a_model_call(self):
        with mock.patch("core.ai.ask", side_effect=AssertionError):
            reply = _ask("jarvis", "   ")
        self.assertTrue(reply.ok)
        self.assertIn("JARVIS", reply.text)

    def test_oversized_command_is_refused_before_the_model(self):
        with mock.patch("core.ai.ask", side_effect=AssertionError):
            reply = _ask("jarvis", "x" * (MAX_COMMAND_CHARS + 1))
        self.assertFalse(reply.ok)

    def test_unknown_target_falls_back_to_the_neutral_gateway(self):
        with mock.patch("core.ai.ask", side_effect=AssertionError):
            reply = route(Request(action="ASK", target="cortana",
                                  command="write a poem"))
        self.assertTrue(reply.ok)
        self.assertEqual(reply.data["target"], "nexus")


class TestDangerousActionsOverWhatsApp(unittest.TestCase):
    """The end-to-end claim: a shutdown asked for over WhatsApp does not
    happen until Bhuvan confirms it."""

    def setUp(self):
        from remote_control import commands, executor
        executor.manager.clear()
        # Scoped stop, not stopall(): the module-level audit redirection must
        # survive, or these tests write into the real remote log.
        patcher = mock.patch.object(
            commands.subprocess, "run",
            return_value=mock.Mock(returncode=0, stdout="", stderr=""))
        self.spawn = patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_shutdown_request_asks_first(self):
        reply = _ask("jarvis", "shutdown")
        self.assertIn("CONFIRM", reply.text)
        self.assertFalse(self.spawn.called)

    def test_confirming_over_the_same_channel_runs_it(self):
        prompt = _ask("jarvis", "shutdown").text
        token = prompt.split("CONFIRM ")[1].split()[0]
        _ask("jarvis", f"CONFIRM {token}")
        self.assertTrue(self.spawn.called)

    def test_the_model_is_never_asked_to_decide_a_shutdown(self):
        with mock.patch("core.ai.ask") as model:
            _ask("friday", "shutdown")
        self.assertFalse(model.called)


class TestAuditing(unittest.TestCase):
    """Routing is recorded, but the conversation itself is not."""

    def test_bhuvan_can_ask_for_the_audit_log_in_plain_english(self):
        for phrasing in ("show me the audit log", "what did you do today",
                         "recent actions", "what happened while I was away"):
            with mock.patch("remote_control.audit.format_recent",
                            return_value="LOG") as fmt:
                reply = route(Request(action="ASK", target="jarvis",
                                      command=phrasing))
            self.assertEqual(reply.text, "LOG", phrasing)
            self.assertTrue(fmt.called, phrasing)

    def test_an_ordinary_message_mentioning_a_log_is_not_an_audit_request(self):
        with mock.patch("remote_control.audit.format_recent") as fmt:
            route(Request(action="ASK", target="nexus",
                          command="is the log file large"))
        self.assertFalse(fmt.called)

    def test_every_route_is_recorded(self):
        with mock.patch("remote_control.audit.record") as rec:
            route(Request(action="ASK", target="jarvis", command="status"))
        self.assertEqual(rec.call_count, 1)
        self.assertEqual(rec.call_args.args[0], "ask")
        self.assertEqual(rec.call_args.kwargs["target"], "jarvis")

    def test_the_command_and_the_reply_are_never_recorded(self):
        secret = "remind me about the hospital appointment on Friday"
        with mock.patch("remote_control.audit.record") as rec:
            route(Request(action="ASK", target="nexus", command=secret))
        written = repr(rec.call_args)
        self.assertNotIn("hospital", written)
        self.assertNotIn("appointment", written)

    def test_a_failed_route_is_recorded_as_failed(self):
        with mock.patch("core.ai.ask", side_effect=RuntimeError("offline")), \
                mock.patch("remote_control.audit.record") as rec:
            route(Request(action="ASK", target="friday", command="tell a joke"))
        self.assertEqual(rec.call_args.args[1], "failed")

    def test_an_audit_failure_does_not_cost_bhuvan_the_answer(self):
        with mock.patch("remote_control.audit.record",
                        side_effect=OSError("disk full")):
            reply = route(Request(action="ASK", target="nexus",
                                  command="what is the battery level"))
        self.assertTrue(reply.ok)
        self.assertTrue(reply.text.strip())


class TestVoicePathUntouched(unittest.TestCase):

    def test_routing_does_not_use_the_live_voice_session(self):
        source = Path(__file__).resolve().parents[1].joinpath(
            "router.py").read_text(encoding="utf-8")
        for forbidden in ("genai", "live_model", "JarvisLive", "speak(",
                          "aio.live"):
            self.assertNotIn(forbidden, source, forbidden)


if __name__ == "__main__":
    unittest.main(verbosity=2)
