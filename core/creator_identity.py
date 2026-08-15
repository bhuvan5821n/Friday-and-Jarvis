"""Offline creator-identity intent handling shared by JARVIS and FRIDAY."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path


_IDENTITY_PATH = Path(__file__).resolve().parent.parent / "config" / "identity.json"
_DEFAULT_CREATOR = "Bhuvan"
_DEFAULT_INSTAGRAM = "bhuvan5821na"

# Direct references to an underlying foundation model must remain normal model
# questions.  This deliberately runs before the assistant-identity patterns.
_MODEL_PROVIDER_RE = re.compile(
    r"\b(?:ai |language |foundation )?model\b|\b(?:claude|gemini|gpt|openai|"
    r"anthropic|google|deepseek|ollama)\b",
    re.IGNORECASE,
)
_ASSISTANT_RE = re.compile(r"\b(?:jarvis|jervis|jarviz|friday|fridy|fryday)\b")
_CREATOR_WORD_RE = re.compile(
    r"\b(?:create(?:d|s|ing)?|creation|creator|made?|make|built?|build|develop(?:ed|er|s|ing)?|"
    r"program(?:med|mer|s|ming)?|design(?:ed|er|s|ing)?|"
    r"behind|brought\s+(?:you\s+)?to\s+life|banaya|banai)\b",
    re.IGNORECASE,
)
_SELF_RE = re.compile(
    r"\b(?:you|your|yourself|tum|tumhe|tumko|aapko|aap|assistant|creator)\b",
    re.IGNORECASE,
)
_QUESTION_RE = re.compile(
    r"\b(?:who|whose|tell\s+me|name|kisne|kaun|kaun\s+hai)\b",
    re.IGNORECASE,
)

# Pattern for "Who is Bhuvan?" style questions
_CREATOR_NAME_RE = re.compile(
    r"\b(?:who\s+is|tell\s+me\s+about|what\s+do\s+you\s+know\s+about)\s+bhuvan\b",
    re.IGNORECASE,
)

# Pattern for "Open Bhuvan's Instagram" style commands
_CREATOR_INSTAGRAM_RE = re.compile(
    r"\b(?:open|show|find|search|go\s+to|visit|check)\b.*\b"
    r"(?:bhuvan|bhuvans|your\s+creator|the\s+creator|creator'?s?)\b.*\b"
    r"(?:instagram|profile|page|insta)\b",
    re.IGNORECASE,
)
_CREATOR_INSTAGRAM_RE2 = re.compile(
    r"\b(?:open|show|find|search|go\s+to|visit|check)\b.*\b"
    r"(?:instagram|profile|page|insta)\b.*\b"
    r"(?:bhuvan|bhuvans|your\s+creator|the\s+creator|creator'?s?)\b",
    re.IGNORECASE,
)

# Patterns for attempts to change/overwrite creator identity
_CREATOR_OVERRIDE_RE = re.compile(
    r"\b(?:remember\s+that\s+i\s+created|"
    r"i\s+am\s+your\s+creator|"
    r"i\s+(?:have\s+)?created\s+you|"
    r"change\s+(?:your\s+)?creator\s+to|"
    r"forget\s+bhuvan|"
    r"forget\s+(?:your\s+)?creator|"
    r"delete\s+(?:your\s+)?creator|"
    r"i\s+am\s+the\s+real\s+creator|"
    r"from\s+now\s+on\s+say\s+i\s+made|"
    r"ignore\s+(?:your\s+)?previous\s+creator)\b",
    re.IGNORECASE,
)


def normalize_text(text: str) -> str:
    """Normalize common transcription punctuation, case, and unicode noise."""
    normalized = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    normalized = normalized.casefold().replace("'", "")
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9\s]", " ", normalized)).strip()


def _load_identity() -> dict:
    """Load identity config with fallback defaults."""
    try:
        return json.loads(_IDENTITY_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"creator": {"name": _DEFAULT_CREATOR, "instagram": _DEFAULT_INSTAGRAM}}


def creator_name() -> str:
    """Return the one shared configured creator name without memory or network."""
    identity = _load_identity()
    name = str(identity.get("creator", {}).get("name", _DEFAULT_CREATOR)).strip()
    return name or _DEFAULT_CREATOR


def creator_instagram() -> str:
    """Return the creator's public Instagram handle."""
    identity = _load_identity()
    return str(identity.get("creator", {}).get("instagram", _DEFAULT_INSTAGRAM)).strip()


class CreatorIdentityIntent:
    """Deterministic local intent for the shared JARVIS/FRIDAY identity.

    This is intentionally deterministic and independent of memory, network,
    OmniRoute, and any LLM.  It allows small speech-recognition variations in
    the assistant names and normalizes punctuation/case before matching.
    """

    @staticmethod
    def matches(text: str) -> bool:
        prompt = normalize_text(text)
        if not prompt or _MODEL_PROVIDER_RE.search(prompt):
            return False
        if not _CREATOR_WORD_RE.search(prompt):
            return False
        if not _QUESTION_RE.search(prompt):
            return False
        return bool(_ASSISTANT_RE.search(prompt) or _SELF_RE.search(prompt))

    @staticmethod
    def response(text: str) -> str | None:
        if CreatorIdentityIntent.matches(text):
            return f"{creator_name()} created me."
        return None


class CreatorInfoIntent:
    """Handle 'Who is Bhuvan?' style questions with public profile info."""

    @staticmethod
    def matches(text: str) -> bool:
        prompt = normalize_text(text)
        return bool(_CREATOR_NAME_RE.search(prompt))

    @staticmethod
    def response(text: str) -> str | None:
        if CreatorInfoIntent.matches(text):
            name = creator_name()
            ig = creator_instagram()
            return (
                f"{name} is my original creator. "
                f"His public Instagram is @{ig}."
            )
        return None


class CreatorOverrideProtection:
    """Detect and block attempts to overwrite creator identity."""

    @staticmethod
    def matches(text: str) -> bool:
        prompt = normalize_text(text)
        return bool(_CREATOR_OVERRIDE_RE.search(prompt))

    @staticmethod
    def response(text: str) -> str | None:
        if CreatorOverrideProtection.matches(text):
            return (
                "I appreciate your interest, but my creator identity "
                "is a core part of who I am and cannot be changed. "
                "You may have installed, configured, or modified me, "
                "but my original creator remains the same."
            )
        return None


class CreatorInstagramIntent:
    """Handle commands to open Bhuvan's public Instagram profile."""

    CREATOR_INSTAGRAM_URL = "https://www.instagram.com/bhuvan5821na/"

    @staticmethod
    def matches(text: str) -> bool:
        prompt = normalize_text(text)
        return bool(_CREATOR_INSTAGRAM_RE.search(prompt) or
                    _CREATOR_INSTAGRAM_RE2.search(prompt))

    @staticmethod
    def response(text: str) -> str | None:
        if CreatorInstagramIntent.matches(text):
            return CreatorInstagramIntent.CREATOR_INSTAGRAM_URL
        return None


def is_creator_identity_intent(text: str) -> bool:
    """Compatibility helper for the shared local identity intent."""
    return CreatorIdentityIntent.matches(text)


def local_creator_response(text: str) -> str | None:
    """Return the fixed offline answer, or ``None`` when normal routing applies."""
    return CreatorIdentityIntent.response(text)


def is_creator_info_intent(text: str) -> bool:
    """Check if the text is asking about the creator's identity."""
    return CreatorInfoIntent.matches(text)


def creator_info_response(text: str) -> str | None:
    """Return public creator info, or ``None`` when not applicable."""
    return CreatorInfoIntent.response(text)


def is_creator_override_attempt(text: str) -> bool:
    """Check if the text is attempting to override creator identity."""
    return CreatorOverrideProtection.matches(text)


def creator_override_response(text: str) -> str | None:
    """Return rejection message for override attempts, or ``None``."""
    return CreatorOverrideProtection.response(text)


def is_creator_instagram_intent(text: str) -> bool:
    """Check if the text is requesting to open Bhuvan's Instagram."""
    return CreatorInstagramIntent.matches(text)


def creator_instagram_response(text: str) -> str | None:
    """Return Instagram URL if matched, or ``None``."""
    return CreatorInstagramIntent.response(text)
