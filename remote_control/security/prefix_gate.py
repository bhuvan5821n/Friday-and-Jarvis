"""Strict prefix gate — deterministic, offline, content-free on rejection.

Bhuvan uses WhatsApp "Message Yourself" as an ordinary notes app. The vast
majority of messages in that chat are personal notes that must stay personal
notes. Only a message that *begins* with an assistant name is an AI request.

This is deliberately plain code, not a model prompt. A prompt can be argued
with; a regex cannot. The gate runs before the LLM, before memory, before
tools, before routing, and before any log line that could contain the text.

Fail-closed in every direction: anything unrecognised, malformed, oversized, or
merely unusual is treated as an ordinary personal note and ignored.
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

#: The only names that turn a self-chat note into a request. Bare and slash
#: forms are both accepted; matching is case-insensitive.
ACCEPTED_PREFIXES: tuple[str, ...] = ("jarvis", "friday", "nexus", "hermes")

#: Which subsystem a prefix addresses. NEXUS and HERMES both mean "the neutral
#: remote gateway" — they are aliases, not separate personalities.
_TARGETS = {"jarvis": "jarvis", "friday": "friday",
            "nexus": "nexus", "hermes": "nexus"}

#: Punctuation that may sit between the name and the command. Anything else
#: (including letters and digits) means the word was not the name — "Jarvista"
#: and "fridays" are ordinary notes.
_SEPARATORS = ",:;.!?-‐‑‒–—―>|"

#: Longest message the gate will even look at. A note larger than this is not a
#: plausible remote command; refusing early also bounds regex work.
MAX_MESSAGE_CHARS = 8192

_PREFIX_RE = re.compile(
    r"^(?P<slash>/)?(?P<name>" + "|".join(ACCEPTED_PREFIXES) + r")"
    r"(?P<sep>[" + re.escape(_SEPARATORS) + r"\s]|$)",
    re.IGNORECASE,
)

# Ignorable characters (zero-width joiners, BOM, bidi marks). These are NOT
# stripped before matching — stripping them would let "J<zwsp>arvis" through.
# They are only detected so such a message is classified as "not addressed".
_INVISIBLE = {"Cf", "Cc"}


@dataclass(frozen=True)
class GateDecision:
    """Outcome of admission control.

    `command` carries user text and therefore must never reach a log. `repr`
    is overridden so an accidental `log.info("%r", decision)` cannot leak it.
    """

    addressed: bool
    target: str | None = None      # "jarvis" | "friday" | "nexus"
    matched_prefix: str | None = None
    command: str = ""
    reason: str = ""               # content-free; safe to log

    def __repr__(self) -> str:            # pragma: no cover - formatting only
        return (f"GateDecision(addressed={self.addressed}, "
                f"target={self.target!r}, reason={self.reason!r}, "
                f"command=<{len(self.command)} chars redacted>)")

    __str__ = __repr__


def _has_leading_invisible(text: str) -> bool:
    """True if the text opens with a format/control character."""
    for ch in text:
        if ch.isspace():
            continue
        return unicodedata.category(ch) in _INVISIBLE
    return False


def parse_prefix(message: object) -> GateDecision:
    """Classify one raw inbound message.

    Returns `addressed=False` for anything that is not unambiguously a command
    addressed to an assistant. The rejection `reason` never contains message
    text, so it is safe to count and log.
    """
    if not isinstance(message, str):
        return GateDecision(False, reason="non_text_message")
    if len(message) > MAX_MESSAGE_CHARS:
        return GateDecision(False, reason="oversized")

    # Leading whitespace is natural typing; leading invisible characters are
    # not, and are the obvious way to smuggle a prefix past a naive matcher.
    if _has_leading_invisible(message):
        return GateDecision(False, reason="leading_invisible_character")

    text = message.lstrip()
    if not text:
        return GateDecision(False, reason="empty")

    match = _PREFIX_RE.match(text)
    if match is None:
        return GateDecision(False, reason="no_prefix")

    name = match.group("name").lower()
    matched = ("/" if match.group("slash") else "") + name
    command = text[match.end():]
    # The match consumed one separator. Any further ones are still addressing
    # ("Jarvis, - status"), so strip them from the front only: trailing
    # punctuation belongs to the command, and dropping it turns Bhuvan's
    # question into a statement before the model ever reads it.
    command = command.strip().lstrip("".join(_SEPARATORS)).strip()

    return GateDecision(
        addressed=True,
        target=_TARGETS[name],
        matched_prefix=matched,
        command=command,
        reason="addressed",
    )


def strip_prefix(message: str) -> str:
    """The command with its addressing prefix removed ("" if not addressed)."""
    return parse_prefix(message).command
