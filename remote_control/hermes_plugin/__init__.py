"""Hardened WhatsApp platform plugin for Bhuvan's laptop.

Subclasses the bundled Hermes WhatsApp adapter and adds one thing: a strict
admission gate in front of `_should_process_message`, which is the first
statement of `_build_message_event` and therefore the last point at which a
message can be dropped before it becomes a `MessageEvent`.

Dropping here means no session, no read receipt, no hook context, no LLM call,
no memory write, no tool execution, and no log line containing the text.

Hermes itself is not modified. This plugin wins by key collision: user plugins
are scanned after bundled ones and later sources override earlier ones.
"""

from .adapter import register

__all__ = ["register"]
