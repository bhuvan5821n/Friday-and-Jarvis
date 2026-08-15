"""Phase 4: routing a remote command to JARVIS, FRIDAY, or NEXUS itself.

Three distinct addressees, deliberately not merged:

  * JARVIS  — the technical assistant, answering in its own voice
  * FRIDAY  — the friendly assistant, answering in its own voice
  * NEXUS   — the neutral gateway; no personality, no character, just facts
              about the machine and the remote channel

The personas are separated the same way the desktop app separates them: by
system prompt. `core/prompt.txt` *is* the JARVIS persona, and FRIDAY layers its
own character on top — this reads both from `main.PERSONAS` rather than
restating them, so a change to either persona reaches WhatsApp automatically
and the two can never drift into one voice.

This does not touch the live voice session. Local conversation keeps using the
Gemini Live model exactly as before; remote text goes through the existing
`core.ai` text path, which is the same router the Chat Studio uses.
"""
from __future__ import annotations

import logging
import re
import time

from . import audit
from .bridge_protocol import Request, Response

log = logging.getLogger("nexus.router")

MAX_COMMAND_CHARS = 4000

#: Answered by the gateway itself. NEXUS is a boundary, not a character: asked
#: what it is, it says so plainly rather than adopting a personality.
_NEXUS_IDENTITY = (
    "I am NEXUS, the remote gateway for this laptop. I am not a separate "
    "assistant — I pass your messages to JARVIS or FRIDAY and bring their "
    "answers back. Address a message to Jarvis or Friday to talk to them.")

_STATUS_RE = re.compile(
    r"\b(status|how are you doing|system|cpu|ram|memory|battery|disk|storage|"
    r"temperature|temp|network|internet|online|uptime)\b", re.IGNORECASE)
_HAPPENING_RE = re.compile(
    r"\b(what(?:'s| is| are you)?\s+(?:happening|going on|you doing|up)|"
    r"doing right now|current task)\b", re.IGNORECASE)
_WHOAMI_RE = re.compile(
    r"\b(who|what)\s+(are|r)\s+(you|u)\b|\bwhat\s+is\s+nexus\b", re.IGNORECASE)
#: Deliberately narrow: this must fire on Bhuvan asking for the remote log, not
#: on the word "log" appearing in an ordinary request.
_AUDIT_RE = re.compile(
    r"\b(audit|audit log|remote log|action log|activity log|"
    r"what did you do|what have you done|recent actions|"
    r"what happened while i was (?:away|out|gone))\b", re.IGNORECASE)

_PERSONA_LABEL = {"jarvis": "JARVIS", "friday": "FRIDAY", "nexus": "NEXUS"}


def _system_prompt(target: str) -> str:
    """The addressed persona's own prompt, read from the app's definition."""
    from pathlib import Path

    base = ""
    try:
        base = (Path(__file__).resolve().parents[1] / "core" / "prompt.txt"
                ).read_text(encoding="utf-8")
    except Exception as exc:
        log.warning("could not read the base persona prompt: %s", exc)

    if target == "friday":
        try:
            import main
            base += main.PERSONAS["friday"]["prompt"]
        except Exception as exc:
            log.warning("could not read the FRIDAY persona: %s", exc)

    return base + (
        "\n[REMOTE CHANNEL]\n"
        "This message arrived over WhatsApp, not by voice. Reply in text only: "
        "no audio cues, no stage directions, no tool calls. Keep it to a few "
        "short lines — it will be read on a phone. You cannot see the screen "
        "or hear anything from here; if you do not know something, say so.\n")


def _local_answer(target: str, command: str) -> str | None:
    """Deterministic answers that must never depend on a model being reachable."""
    from core.creator_identity import local_creator_response
    from .status import format_status, whats_happening

    identity = local_creator_response(command)
    if identity is not None:
        return identity

    # ponytail: commands and confirmations are answered by deterministic code,
    # never by the model — a shutdown must not depend on how an LLM read a
    # sentence.
    from .executor import handle as execute
    acted = execute(command)
    if acted is not None:
        return acted

    if _HAPPENING_RE.search(command):
        return whats_happening()
    if _AUDIT_RE.search(command):
        from .audit import format_recent
        return format_recent()
    if _STATUS_RE.search(command):
        return format_status()

    if target == "nexus" and _WHOAMI_RE.search(command):
        return _NEXUS_IDENTITY
    return None


def route(request: Request) -> Response:
    """Answer one remote command, and record that it happened.

    The audit entry names the assistant and where the answer came from. It
    never carries the command or the reply: what was asked is between Bhuvan
    and his assistant, and the log exists to show what the laptop *did*.
    """
    started = time.monotonic()
    response = _route(request)
    try:
        target = (response.data or {}).get("target") or request.target or "nexus"
        audit.record("ask", "ok" if response.ok else "failed", target=target,
                     detail=(response.data or {}).get("source", ""),
                     duration_ms=int((time.monotonic() - started) * 1000))
    except Exception:
        # ponytail: the answer is already computed. Losing the record is bad;
        # losing the reply because of it is worse.
        log.exception("audit record failed")
    return response


def _route(request: Request) -> Response:
    """Answer one remote command as the addressed assistant."""
    command = (request.command or "").strip()
    target = request.target if request.target in _PERSONA_LABEL else "nexus"

    if not command:
        return Response(ok=True, request_id=request.request_id,
                        text=f"{_PERSONA_LABEL[target]} here. What do you need?")
    if len(command) > MAX_COMMAND_CHARS:
        return Response(ok=False, request_id=request.request_id,
                        error="That message is too long to handle remotely.")

    try:
        local = _local_answer(target, command)
    except Exception:
        log.exception("local answer failed")
        local = None
    if local is not None:
        from .executor import take_attachment
        data = {"target": target, "source": "local"}
        attachment = take_attachment()
        if attachment:
            data["attachment"] = attachment
        return Response(ok=True, text=local, request_id=request.request_id,
                        data=data)

    if target == "nexus":
        # NEXUS has no personality and no model of its own. Anything it cannot
        # answer from the machine belongs to an assistant.
        return Response(ok=True, request_id=request.request_id,
                        data={"target": target, "source": "local"},
                        text="I only handle status and the remote channel "
                             "itself. Start your message with Jarvis or Friday "
                             "and I will pass it on.")

    try:
        from core import ai
        text = ai.ask(command, system=_system_prompt(target))
    except Exception as exc:
        log.warning("remote routing to %s failed: %s", target, exc)
        return Response(ok=False, request_id=request.request_id,
                        error=f"{_PERSONA_LABEL[target]} could not answer right "
                              f"now — the model service is not reachable.")

    text = (text or "").strip()
    if not text:
        return Response(ok=False, request_id=request.request_id,
                        error=f"{_PERSONA_LABEL[target]} returned an empty "
                              f"answer.")
    return Response(ok=True, text=text, request_id=request.request_id,
                    data={"target": target, "source": "model"})
