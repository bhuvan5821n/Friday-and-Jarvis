"""Phase 11: the one place a remote action actually happens.

Everything dangerous flows through `handle`, so the safety rules are enforced
once rather than repeated in every capability:

    message ─▶ confirmation reply?  ─▶ confirm/cancel the pending action
             └▶ approved command?   ─▶ safe: run now
                                    └▶ dangerous: park it, ask, run only on
                                       CONFIRM with the matching token

A dangerous command cannot reach `commands.run` without `allow_dangerous`, and
only this module passes it — after the confirmation manager has handed back the
pending action. Every outcome is audited.
"""
from __future__ import annotations

import logging
import re

from . import audit
from .commands import CommandError, SHELL_REFUSAL, describe, lookup, run
from .confirmation import (ConfirmationError, ConfirmationManager,
                           parse_confirmation)

log = logging.getLogger("nexus.executor")

_SCREENSHOT_RE = re.compile(
    r"\b(screenshot|screen shot|screen grab|show me (?:the |your )?screen|"
    r"what(?:'s| is) on (?:the |my )?screen)\b", re.IGNORECASE)

#: One pending action for the one authorized user.
manager = ConfirmationManager()


#: Set by the transport so an image can actually be delivered. Without it the
#: capability degrades to a description rather than pretending it sent a photo.
_image_sender = None

#: Where a capability parks bytes for the current request to carry back. The
#: bridge is request/response, so an image rides home with its own reply rather
#: than needing a second channel.
_attachment: dict | None = None


def take_attachment() -> dict | None:
    """Claim anything the last action produced. Clears it."""
    global _attachment
    attachment, _attachment = _attachment, None
    return attachment


def set_image_sender(sender) -> None:
    """Register `sender(png_bytes, caption) -> bool` for image delivery."""
    global _image_sender
    _image_sender = sender


def _screenshot() -> str:
    global _attachment
    from .screenshot import ScreenshotError, capture
    from .screenshot import describe as describe_shot

    try:
        data, meta = capture()
    except ScreenshotError as exc:
        audit.record("screenshot", "refused", detail=str(exc)[:120])
        return str(exc)

    caption = describe_shot(meta)
    sent = False
    if _image_sender is not None:
        try:
            sent = bool(_image_sender(data, caption))
        except Exception:
            log.exception("image delivery failed")
    else:
        # ponytail: no local sender, so the bytes ride home with the reply —
        # the bridge is request/response and has no second channel to the phone.
        import base64
        _attachment = {"kind": "image/png", "caption": caption,
                       "b64": base64.b64encode(data).decode("ascii")}
        sent = True

    audit.record("screenshot", "ok" if sent else "failed",
                 detail=f"{meta['width']}x{meta['height']}, {meta['bytes']}B")
    if sent:
        return caption
    return (f"{caption} I could not attach the image, so it was discarded "
            f"rather than left on the laptop.")


def _execute(action: str, params: dict, *, confirmed: bool) -> str:
    try:
        text = run(action, params.get("arg"), allow_dangerous=confirmed)
    except CommandError as exc:
        audit.record(action, "refused", detail=str(exc)[:120],
                     confirmed=confirmed)
        return str(exc)
    audit.record(action, "confirmed" if confirmed else "ok", confirmed=confirmed)
    return text


def handle(message: str) -> str | None:
    """Answer a command message, or return None if it is not one.

    None means "this was not a command" — the caller falls through to the
    assistant. That keeps ordinary conversation out of this path entirely.
    """
    text = str(message or "").strip()
    if not text:
        return None

    verb, token = parse_confirmation(text)
    if verb == "cancel":
        pending = manager.cancel()
        if pending is None:
            return "There is nothing waiting for confirmation."
        audit.record(pending.action, "cancelled")
        return f"Cancelled. {pending.summary} was not done."

    if verb == "confirm":
        try:
            pending = manager.confirm(token)
        except ConfirmationError as exc:
            audit.record("confirm", "expired", detail=str(exc)[:120])
            return str(exc)
        return _execute(pending.action, pending.params, confirmed=True)

    if text.lower() in ("commands", "help", "what can you do",
                        "what can i do", "list commands"):
        return describe()

    if _SCREENSHOT_RE.search(text):
        return _screenshot()

    # A command is the first word; anything after it must match a closed set of
    # choices, so the rest of the message is never a free-form argument.
    head, _, tail = text.partition(" ")
    command = lookup(head)
    if command is None:
        return None

    arg = tail.strip() or None
    if command.dangerous:
        pending = manager.request(command.name, command.summary + "?",
                                  {"arg": arg} if arg else {})
        audit.record(command.name, "refused", detail="awaiting confirmation")
        return manager.prompt_text(pending)

    return _execute(command.name, {"arg": arg} if arg else {}, confirmed=False)


def refuse_shell(text: str) -> str:
    """The fixed refusal, for callers that detect a shell attempt themselves."""
    audit.record("shell", "refused", detail="free-form shell text")
    return SHELL_REFUSAL
