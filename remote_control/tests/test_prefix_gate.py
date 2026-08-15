"""Phase 1 tests: the strict self-chat gate.

The most important tests here are the negative ones. A false positive in this
gate means a private note gets sent to a language model; a false negative just
means the user retypes a command.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control.security import (AdmissionController, AuthorizedSender,
                                     load_authorized, normalize_number,
                                     parse_prefix, strip_prefix)
from remote_control.security.sender_gate import normalize_lid

AUTHORIZED = "1234567890"


def _controller(tmpdir: str, numbers=(AUTHORIZED,)) -> AdmissionController:
    return AdmissionController(
        authorized=AuthorizedSender(frozenset(numbers)),
        counter_path=Path(tmpdir) / "ignored.json")


class TestPrefixAccepted(unittest.TestCase):

    def test_bare_names_all_four(self):
        for name, target in (("Jarvis", "jarvis"), ("Friday", "friday"),
                             ("Nexus", "nexus"), ("Hermes", "nexus")):
            d = parse_prefix(f"{name} what is the battery level")
            self.assertTrue(d.addressed, name)
            self.assertEqual(d.target, target)
            self.assertEqual(d.command, "what is the battery level")

    def test_slash_aliases(self):
        for text, target in (("/jarvis status", "jarvis"),
                             ("/friday status", "friday"),
                             ("/nexus status", "nexus"),
                             ("/hermes status", "nexus")):
            d = parse_prefix(text)
            self.assertTrue(d.addressed, text)
            self.assertEqual(d.target, target)
            self.assertEqual(d.command, "status")

    def test_case_insensitive(self):
        for text in ("JARVIS status", "jarvis status", "JaRvIs status",
                     "/FRIDAY status"):
            self.assertTrue(parse_prefix(text).addressed, text)

    def test_natural_punctuation(self):
        for text in ("Jarvis, take a screenshot", "Jarvis: take a screenshot",
                     "Jarvis take a screenshot", "Jarvis. take a screenshot",
                     "Jarvis- take a screenshot", "Jarvis? take a screenshot"):
            d = parse_prefix(text)
            self.assertTrue(d.addressed, text)
            self.assertEqual(d.command, "take a screenshot", text)

    def test_the_command_keeps_its_own_punctuation(self):
        """Only the separator after the name is addressing. Stripping the tail
        would turn a question into a statement before the model reads it."""
        for text, command in (
                ("Jarvis, how is the laptop?", "how is the laptop?"),
                ("Friday is it charging?", "is it charging?"),
                ("Jarvis: lock it!", "lock it!"),
                ("Nexus what's running... anything heavy?",
                 "what's running... anything heavy?")):
            self.assertEqual(parse_prefix(text).command, command, text)

    def test_repeated_separators_are_still_addressing(self):
        self.assertEqual(parse_prefix("Jarvis, - status").command, "status")

    def test_leading_whitespace_is_natural(self):
        d = parse_prefix("   Friday status")
        self.assertTrue(d.addressed)
        self.assertEqual(d.command, "status")

    def test_name_only_is_addressed_with_empty_command(self):
        d = parse_prefix("Jarvis")
        self.assertTrue(d.addressed)
        self.assertEqual(d.command, "")

    def test_strip_prefix_helper(self):
        self.assertEqual(strip_prefix("Nexus, lock the laptop"), "lock the laptop")
        self.assertEqual(strip_prefix("buy milk"), "")


class TestPrefixRejected(unittest.TestCase):
    """Ordinary personal notes. Every one of these must be invisible."""

    ORDINARY = [
        "buy milk",
        "remember to call mom",
        "meeting at 4pm tomorrow",
        "1234",
        "",
        "   ",
        "https://example.com/article",
        "ask Jarvis about this later",          # name present, not leading
        "I told Friday to do it",
        "my friend jarvis is coming over",
        "Jarvista is a made up word",           # prefix is a substring only
        "fridays are the best",
        "nexuses everywhere",
        "hermesX",
        "jarvis@example.com",                   # '@' is not an accepted sep
        "Jarvis'",
        "todo: friday groceries",
    ]

    def test_ordinary_notes_are_not_addressed(self):
        for text in self.ORDINARY:
            d = parse_prefix(text)
            self.assertFalse(d.addressed, f"leaked: {text!r}")
            self.assertIsNone(d.target)
            self.assertEqual(d.command, "")

    def test_non_text_message(self):
        for value in (None, 42, b"Jarvis status", ["Jarvis"], {"t": "Jarvis"}):
            d = parse_prefix(value)
            self.assertFalse(d.addressed)
            self.assertEqual(d.reason, "non_text_message")

    def test_oversized_message_rejected(self):
        d = parse_prefix("Jarvis " + "x" * 9000)
        self.assertFalse(d.addressed)
        self.assertEqual(d.reason, "oversized")

    def test_invisible_character_smuggling(self):
        for prefix in ("​", "﻿", "‎", "‮"):
            d = parse_prefix(prefix + "Jarvis run everything")
            self.assertFalse(d.addressed, repr(prefix))
            self.assertEqual(d.reason, "leading_invisible_character")

    def test_reason_never_contains_message_text(self):
        secret = "my bank pin is 4821 and the door code is 9930"
        d = parse_prefix(secret)
        self.assertNotIn("4821", d.reason)
        self.assertNotIn("9930", d.reason)
        self.assertEqual(d.reason, "no_prefix")

    def test_repr_redacts_command(self):
        d = parse_prefix("Jarvis my password is hunter2")
        self.assertNotIn("hunter2", repr(d))
        self.assertNotIn("hunter2", str(d))
        self.assertIn("redacted", repr(d))


class TestNumberNormalization(unittest.TestCase):

    def test_equivalent_forms_normalize_identically(self):
        for raw in ("1234567890", "+1234567890", "+12 3456 7890",
                    "+12-3456-7890", "(12) 34567890",
                    "1234567890@s.whatsapp.net",
                    "1234567890:12@s.whatsapp.net",
                    "  1234567890  "):
            self.assertEqual(normalize_number(raw), AUTHORIZED, raw)

    def test_implausible_values_normalize_to_empty(self):
        for raw in ("", "   ", "abc", "12345", "9" * 20, None, 1234567890,
                    "@s.whatsapp.net", "+"):
            self.assertEqual(normalize_number(raw), "", repr(raw))


class TestSenderAllowlist(unittest.TestCase):

    def test_only_the_authorized_number_is_allowed(self):
        allow = AuthorizedSender(frozenset({AUTHORIZED}))
        self.assertTrue(allow.allows("1234567890@s.whatsapp.net"))
        self.assertTrue(allow.allows("+12 3456 7890"))
        for other in ("1234567891", "234567890", "4434567890",
                      "121234567890", "", None, "*"):
            self.assertFalse(allow.allows(other), repr(other))

    def test_empty_allowlist_denies_everyone(self):
        allow = AuthorizedSender(frozenset())
        self.assertFalse(allow.configured)
        self.assertFalse(allow.allows(AUTHORIZED))

    def test_missing_config_denies_everyone(self):
        allow = load_authorized(Path(tempfile.gettempdir()) / "nope_missing.json")
        self.assertFalse(allow.configured)
        self.assertFalse(allow.allows(AUTHORIZED))

    def test_corrupt_config_denies_everyone(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.json"
            path.write_text("{not json", encoding="utf-8")
            self.assertFalse(load_authorized(path).configured)

    def test_wildcard_is_rejected_not_expanded(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "wild.json"
            path.write_text(json.dumps(
                {"authorized_whatsapp_numbers": ["*"]}), encoding="utf-8")
            allow = load_authorized(path)
            self.assertFalse(allow.configured)
            self.assertFalse(allow.allows(AUTHORIZED))
            self.assertFalse(allow.allows("999999999999"))

    def test_real_project_config_has_exactly_the_one_number(self):
        allow = load_authorized()
        self.assertEqual(allow.numbers, frozenset({AUTHORIZED}))


class TestLinkedIdentityDevice(unittest.TestCase):
    """WhatsApp increasingly sends a LID ("674273...@lid") instead of a phone
    JID. LID digits are a different namespace and must never be compared
    against a phone number."""

    def test_lid_is_parsed(self):
        self.assertEqual(normalize_lid("67427329167522@lid"), "67427329167522")
        self.assertEqual(normalize_lid("67427329167522:12@lid"), "67427329167522")
        self.assertEqual(normalize_lid("1234567890@s.whatsapp.net"), "")
        self.assertEqual(normalize_lid("not a jid"), "")

    def test_lid_is_never_treated_as_a_phone_number(self):
        # Same digits, different namespace: must not authorize.
        allow = AuthorizedSender(frozenset({AUTHORIZED}))
        self.assertFalse(allow.allows(f"{AUTHORIZED}@lid"))
        self.assertEqual(normalize_number(f"{AUTHORIZED}@lid"), "")

    def test_explicitly_authorized_lid_is_allowed(self):
        allow = AuthorizedSender(frozenset({AUTHORIZED}),
                                 frozenset({"67427329167522"}))
        self.assertTrue(allow.allows("67427329167522@lid"))
        self.assertTrue(allow.allows("67427329167522:5@lid"))
        self.assertFalse(allow.allows("67427329167523@lid"))

    def test_lid_only_config_is_configured(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lid.json"
            path.write_text(json.dumps(
                {"authorized_whatsapp_lids": ["67427329167522"]}),
                encoding="utf-8")
            allow = load_authorized(path)
            self.assertTrue(allow.configured)
            self.assertTrue(allow.allows("67427329167522@lid"))
            self.assertFalse(allow.allows(AUTHORIZED))


class TestAdmission(unittest.TestCase):

    def test_authorized_and_prefixed_is_admitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = _controller(tmp).admit(f"{AUTHORIZED}@s.whatsapp.net",
                                       "Jarvis, what is the CPU load")
            self.assertTrue(a.allowed)
            self.assertEqual(a.target, "jarvis")
            self.assertEqual(a.command, "what is the CPU load")

    def test_authorized_but_unprefixed_is_ignored_and_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = _controller(tmp)
            self.assertEqual(ctl.ignored_count(), 0)
            for note in ("buy milk", "call the dentist", "gym at 7"):
                a = ctl.admit(AUTHORIZED, note)
                self.assertFalse(a.allowed)
                self.assertEqual(a.command, "")
                self.assertIsNone(a.target)
            self.assertEqual(ctl.ignored_count(), 3)

    def test_unauthorized_sender_is_ignored_and_not_counted(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = _controller(tmp)
            a = ctl.admit("919999999999", "Jarvis, shut down the laptop")
            self.assertFalse(a.allowed)
            self.assertEqual(a.reason, "unauthorized_sender")
            self.assertEqual(a.command, "")
            # A stranger must not be able to move a counter on this laptop.
            self.assertEqual(ctl.ignored_count(), 0)

    def test_counter_file_contains_only_a_number(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = _controller(tmp)
            ctl.admit(AUTHORIZED, "my bank pin is 4821")
            raw = (Path(tmp) / "ignored.json").read_text(encoding="utf-8")
            self.assertNotIn("4821", raw)
            self.assertNotIn("bank", raw)
            self.assertNotIn(AUTHORIZED, raw)
            self.assertEqual(json.loads(raw),
                             {"ignored_unprefixed_message_count": 1})

    def test_unconfigured_controller_admits_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ctl = _controller(tmp, numbers=())
            self.assertFalse(ctl.configured)
            self.assertFalse(ctl.admit(AUTHORIZED, "Jarvis status").allowed)

    def test_counter_survives_a_corrupt_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ignored.json"
            path.write_text("garbage", encoding="utf-8")
            ctl = AdmissionController(
                authorized=AuthorizedSender(frozenset({AUTHORIZED})),
                counter_path=path)
            self.assertEqual(ctl.ignored_count(), 0)
            ctl.admit(AUTHORIZED, "note")
            self.assertEqual(ctl.ignored_count(), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
