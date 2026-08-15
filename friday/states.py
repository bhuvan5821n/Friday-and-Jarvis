"""Mapping from FRIDAY's operational states to what the interface shows.

This is the single place that decides which clip plays, what the status badge
reads, and which accent colour the surround uses.  Panels and the avatar both
read from here, so a state can never mean two different things in two places.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StateVisual:
    """How one operational state presents itself."""

    emotion: str
    status: str
    accent: str = "#00d4ff"
    #: States that are momentary: the UI returns to ``revert_to`` afterwards.
    hold_ms: int = 0
    revert_to: str = ""


#: Amber and crimson are reserved for genuine warnings and errors so that a
#: coloured interface always means something.
CYAN = "#00d4ff"
VIOLET = "#8b5cf6"
GREEN = "#22c55e"
AMBER = "#ffb020"
CRIMSON = "#ef4444"

STATE_VISUALS: dict[str, StateVisual] = {
    "sleeping":          StateVisual("neutral", "STANDBY", "#2a4a63"),
    "waking":            StateVisual("neutral", "WAKING", CYAN),
    "idle":              StateVisual("neutral", "READY", CYAN),
    "attentive":         StateVisual("listening", "ATTENTIVE", CYAN),
    "listening":         StateVisual("listening", "LISTENING", CYAN),
    "hearing":           StateVisual("listening", "HEARING YOU", CYAN),
    "processing_speech": StateVisual("thinking", "PROCESSING SPEECH", VIOLET),
    "thinking":          StateVisual("thinking", "THINKING", VIOLET),
    "routing_model":     StateVisual("thinking_alt", "SELECTING MODEL", VIOLET),
    "streaming":         StateVisual("thinking_alt", "RESPONDING", CYAN),
    "executing_tool":    StateVisual("thinking_alt", "EXECUTING TASK", VIOLET),
    "speaking":          StateVisual("speaking", "SPEAKING", CYAN),
    "curious":           StateVisual("curious", "ANALYZING", CYAN),
    "surprised":         StateVisual("surprised", "…", CYAN, hold_ms=2000, revert_to="idle"),
    "success":           StateVisual("happy", "COMPLETED", GREEN, hold_ms=2600, revert_to="idle"),
    "warning":           StateVisual("neutral", "WARNING", AMBER),
    "error":             StateVisual("neutral", "ERROR", CRIMSON),
    "reconnecting":      StateVisual("thinking_alt", "RECONNECTING", AMBER),
    "muted":             StateVisual("neutral", "MICROPHONE MUTED", "#2a4a63"),
}

DEFAULT_STATE = "idle"


def visual_for(state: str) -> StateVisual:
    """Presentation for ``state``, falling back to idle for unknown labels."""
    return STATE_VISUALS.get(state, STATE_VISUALS[DEFAULT_STATE])
