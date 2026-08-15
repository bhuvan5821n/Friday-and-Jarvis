"""Phase 1 integration test: the gate as Hermes actually sees it.

Runs inside the *Hermes* virtualenv (which has yaml, pydantic, etc.) and loads
the real plugin, the real bundled adapter class, and the real admission gate.
It never opens a socket, never touches the WhatsApp session,
and never starts the gateway.

The claim under test: for a message that is not addressed to an assistant,
`_should_process_message` returns False *without* consulting Hermes' own policy
— so no MessageEvent is ever built, and nothing downstream can run.

Run with the Hermes virtualenv Python interpreter on this test file.
"""
from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

HERMES_AGENT = Path(r"D:\hermes ai\hermes-agent")
PLUGIN_DIR = Path(r"D:\hermes ai\plugins\whatsapp")
AUTHORIZED = "1234567890"

if str(HERMES_AGENT) not in sys.path:
    sys.path.insert(0, str(HERMES_AGENT))


def _load_plugin():
    spec = importlib.util.spec_from_file_location(
        "nexus_wa_plugin", PLUGIN_DIR / "__init__.py",
        submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    module.__package__ = "nexus_wa_plugin"
    sys.modules["nexus_wa_plugin"] = module
    spec.loader.exec_module(module)
    return importlib.import_module("nexus_wa_plugin.adapter")


plugin_adapter = _load_plugin()


class _Adapter(plugin_adapter.GatedWhatsAppAdapter):
    """Real gate, real class, no network. `__init__` is bypassed so no bridge
    process, no HTTP session, and no config are required."""

    def __init__(self):
        self._admission = plugin_adapter.AdmissionController()
        self.super_calls = 0

    def _super_should_process(self, data):
        self.super_calls += 1
        return True

    # Stand in for Hermes' own policy so we can prove ours runs first and can
    # only ever subtract. The real super() is exercised in test_super_is_real.
    def _should_process_message(self, data):
        import types
        original = plugin_adapter.WhatsAppAdapter._should_process_message
        try:
            plugin_adapter.WhatsAppAdapter._should_process_message = (
                lambda self, d: self._super_should_process(d))
            return super()._should_process_message(data)
        finally:
            plugin_adapter.WhatsAppAdapter._should_process_message = original


def _msg(body, sender=f"{AUTHORIZED}@s.whatsapp.net"):
    return {"senderId": sender, "chatId": f"{AUTHORIZED}@s.whatsapp.net",
            "body": body, "isGroup": False, "messageId": "TEST1"}


class TestGateInHermes(unittest.TestCase):

    def setUp(self):
        self.adapter = _Adapter()

    def test_gate_is_configured(self):
        self.assertTrue(self.adapter._admission.configured,
                        "allowlist missing — the gate would ignore everything")

    def test_addressed_message_passes_through(self):
        for body, target in (("Jarvis status", "jarvis"),
                             ("/friday battery", "friday"),
                             ("Nexus, lock", "nexus"),
                             ("Hermes ping", "nexus")):
            data = _msg(body)
            self.assertTrue(self.adapter._should_process_message(data), body)
            self.assertEqual(data["nexusTarget"], target)

    def test_routing_metadata_is_attached(self):
        data = _msg("Jarvis, take a screenshot")
        self.adapter._should_process_message(data)
        self.assertEqual(data["nexusTarget"], "jarvis")
        self.assertEqual(data["nexusCommand"], "take a screenshot")

    def test_ordinary_note_is_dropped_before_hermes_policy(self):
        before = self.adapter.super_calls
        for note in ("buy milk", "call mom at 6", "gym tomorrow",
                     "meeting notes: budget approved", "1234"):
            data = _msg(note)
            self.assertFalse(self.adapter._should_process_message(data), note)
            self.assertNotIn("nexusTarget", data)
        # Hermes' own policy was never even asked — we short-circuited first.
        self.assertEqual(self.adapter.super_calls, before)

    def test_unauthorized_sender_is_dropped_even_when_addressed(self):
        data = _msg("Jarvis, shut down the laptop",
                    sender="919999999999@s.whatsapp.net")
        self.assertFalse(self.adapter._should_process_message(data))
        self.assertEqual(self.adapter.super_calls, 0)

    def test_media_without_caption_is_dropped(self):
        data = _msg("")
        data.update(hasMedia=True, mediaType="image/jpeg", body="")
        self.assertFalse(self.adapter._should_process_message(data))

    def test_broken_gate_fails_closed(self):
        class Exploding:
            configured = True

            def admit(self, sender, message):
                raise RuntimeError("gate is broken")

        self.adapter._admission = Exploding()
        self.assertFalse(self.adapter._should_process_message(_msg("Jarvis status")))
        self.assertEqual(self.adapter.super_calls, 0)

    def test_super_is_real_and_can_still_deny(self):
        """Our gate only subtracts: a message we admit is still subject to
        Hermes' own rules (here, a broadcast/status pseudo-chat)."""
        adapter = plugin_adapter.GatedWhatsAppAdapter.__new__(
            plugin_adapter.GatedWhatsAppAdapter)
        adapter._admission = plugin_adapter.AdmissionController()
        data = _msg("Jarvis status")
        data["chatId"] = "status@broadcast"
        self.assertFalse(adapter._should_process_message(data))


class _ReplyAdapter(plugin_adapter.GatedWhatsAppAdapter):
    """Real handle_message, no socket. Records what would have been sent."""

    def __init__(self):
        self.sent = []

    async def send(self, chat_id, content, **kwargs):
        self.sent.append((chat_id, content))
        return None


class _Source:
    def __init__(self, chat_id):
        self.chat_id = chat_id


class _Event:
    def __init__(self, raw):
        self.raw_message = raw
        self.source = _Source(f"{AUTHORIZED}@s.whatsapp.net")


class TestReplyPath(unittest.TestCase):
    """Phase 4 delivery: an addressed message is answered by JARVIS/FRIDAY over
    the NEXUS bridge, and Hermes' own agent never sees it."""

    def setUp(self):
        self.adapter = _ReplyAdapter()
        self.agent_calls = []
        original = plugin_adapter.WhatsAppAdapter.handle_message

        async def spy(inner_self, event):
            self.agent_calls.append(event)

        plugin_adapter.WhatsAppAdapter.handle_message = spy
        self.addCleanup(setattr, plugin_adapter.WhatsAppAdapter,
                        "handle_message", original)

    def _run(self, event):
        import asyncio
        asyncio.run(self.adapter.handle_message(event))

    def _admitted(self, body):
        data = _msg(body)
        gate = plugin_adapter.AdmissionController().admit(data["senderId"], body)
        self.assertTrue(gate.allowed, body)
        data["nexusTarget"] = gate.target
        data["nexusCommand"] = gate.command
        return _Event(data)

    def test_an_addressed_message_is_answered_over_the_bridge(self):
        asked = []

        def fake_ask(target, command):
            asked.append((target, command))
            return "All good here.", None

        self.adapter._ask_assistant = fake_ask
        self._run(self._admitted("Friday, how is the laptop?"))
        self.assertEqual(asked, [("friday", "how is the laptop?")])
        self.assertEqual(self.adapter.sent,
                         [(f"{AUTHORIZED}@s.whatsapp.net", "All good here.")])

    def test_a_screenshot_is_delivered_as_an_image_not_as_text(self):
        import base64
        png = b"\x89PNG-pretend"
        self.adapter._ask_assistant = lambda t, c: (
            "Screen: 1280x720.",
            {"kind": "image/png", "caption": "Screen: 1280x720.",
             "b64": base64.b64encode(png).decode("ascii")})
        images = []

        async def fake_send_png(chat_id, data, caption):
            images.append((data, caption))
            return True

        self.adapter._send_png = fake_send_png
        self._run(self._admitted("Jarvis screenshot"))
        self.assertEqual(images, [(png, "Screen: 1280x720.")])
        self.assertEqual(self.adapter.sent, [],
                         "the image was also sent as text")

    def test_the_hermes_agent_never_sees_the_message(self):
        self.adapter._ask_assistant = lambda t, c: ("ok", None)
        self._run(self._admitted("Jarvis status"))
        self.assertEqual(self.agent_calls, [],
                         "Hermes' own agent loop was invoked; it must not be")

    def test_a_message_without_a_routing_target_is_dropped(self):
        called = []
        self.adapter._ask_assistant = lambda t, c: (called.append(1), "ok", None)[1:]
        self._run(_Event({"senderId": AUTHORIZED, "body": "buy milk"}))
        self.assertEqual(called, [])
        self.assertEqual(self.adapter.sent, [])
        self.assertEqual(self.agent_calls, [])

    def test_a_bridge_failure_is_reported_honestly_not_silently(self):
        def boom(target, command):
            raise RuntimeError("bridge exploded")

        self.adapter._ask_assistant = boom
        self._run(self._admitted("Jarvis status"))
        self.assertEqual(len(self.adapter.sent), 1)
        reply = self.adapter.sent[0][1]
        self.assertIn("went wrong", reply.lower())
        # The failure detail is for the log, not for the phone.
        self.assertNotIn("exploded", reply)

    def test_the_real_bridge_client_loads_inside_the_hermes_venv(self):
        """The client is pure stdlib precisely so this import works here."""
        client_module = plugin_adapter._load_bridge_client()
        self.assertTrue(hasattr(client_module, "BridgeClient"))
        self.assertTrue(client_module.OFFLINE_MESSAGE)

    def test_an_offline_assistant_gives_a_plain_answer_not_a_crash(self):
        """Nothing is listening on the bridge port during tests, so this is the
        real client against a real closed port."""
        reply, attachment = self.adapter._ask_assistant("jarvis", "status")
        self.assertIsInstance(reply, str)
        self.assertTrue(reply.strip())
        self.assertIsNone(attachment)


class TestSourceUnmodified(unittest.TestCase):
    """The Hermes installation must stay untouched — the gate lives in a user
    plugin and in Bhuvan's project, nowhere else."""

    def test_bundled_adapter_has_no_nexus_references(self):
        for rel in ("plugins/platforms/whatsapp/adapter.py",
                    "gateway/platforms/whatsapp_common.py",
                    "scripts/whatsapp-bridge/bridge.js"):
            text = (HERMES_AGENT / rel).read_text(encoding="utf-8",
                                                  errors="replace")
            self.assertNotIn("nexus", text.lower(), f"{rel} was modified")

    def test_deployed_plugin_matches_the_version_controlled_copy(self):
        """The deployed plugin is outside this repo, so it can drift out of
        review. It must not."""
        tracked = Path(__file__).resolve().parents[1] / "hermes_plugin"
        for name in ("adapter.py", "__init__.py", "plugin.yaml"):
            deployed = (PLUGIN_DIR / name).read_text(encoding="utf-8")
            in_repo = (tracked / name).read_text(encoding="utf-8")
            self.assertEqual(
                deployed, in_repo,
                f"{name} differs between {PLUGIN_DIR} and {tracked}; "
                "re-deploy or commit the change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
