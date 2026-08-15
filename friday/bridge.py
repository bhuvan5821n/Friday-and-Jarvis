"""Wiring between FRIDAY's backend and her interface.

Every visible reaction originates in a real event: a wake word actually fired,
the microphone really produced that amplitude, TTS genuinely started.  The only
timers in the interface drive idle motion and the crossfade.

All backend callers may be on worker threads, so every entry point here is a
Qt signal.  Nothing touches a widget from a non-GUI thread.
"""
from __future__ import annotations

import logging

from PyQt6.QtCore import QObject, pyqtSignal, pyqtSlot

log = logging.getLogger("friday.bridge")

#: Backend lifecycle labels -> FRIDAY states. Anything unrecognised is ignored
#: rather than guessed at, so a new backend string cannot silently mean "idle".
RUNTIME_STATES = {
    "INITIALISING": "idle",
    "IDLE": "idle",
    "READY": "idle",
    "MUTED": "muted",
    "SLEEPING": "sleeping",
    "WAKING": "waking",
    "LISTENING": "listening",
    "HEARING": "hearing",
    "PROCESSING": "processing_speech",
    "TRANSCRIBING": "processing_speech",
    "ROUTING": "routing_model",
    "THINKING": "thinking",
    "STREAMING": "streaming",
    "SPEAKING": "speaking",
    "EXECUTING": "executing_tool",
    "SUCCESS": "success",
    "RECONNECTING": "reconnecting",
    "WARNING": "warning",
    "ERROR": "error",
}


class FridayBridge(QObject):
    """Thread-safe funnel from backend events into the FRIDAY window.

    The backend calls the plain methods (from any thread); the window connects
    to the signals.  Qt queues each emission onto the GUI thread.
    """

    state_requested = pyqtSignal(str)
    mic_level = pyqtSignal(float)
    tts_level = pyqtSignal(float)
    vad_probability = pyqtSignal(float)
    wake_word_armed = pyqtSignal(bool)
    muted = pyqtSignal(bool)
    reply_text = pyqtSignal(str)
    stream_token = pyqtSignal(str)
    connection = pyqtSignal(bool)
    present_window = pyqtSignal()

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self._stream_buffer = ""
        self._speaking_return_state = "idle"

    # ---- microphone / wake word ----------------------------------------

    def on_wake_word(self) -> None:
        """A wake word actually fired: surface the one window and listen."""
        self.present_window.emit()
        self.state_requested.emit("listening")

    def on_mic_level(self, level: float) -> None:
        self.mic_level.emit(max(0.0, min(1.0, float(level))))

    def on_voice_activity(self, active: bool, probability: float | None = None) -> None:
        if probability is not None:
            self.vad_probability.emit(max(0.0, min(1.0, float(probability))))
        self.state_requested.emit("hearing" if active else "processing_speech")

    def on_mic_muted(self, muted: bool) -> None:
        self.muted.emit(bool(muted))
        self.state_requested.emit("muted" if muted else "idle")

    def on_wake_word_armed(self, armed: bool) -> None:
        self.wake_word_armed.emit(bool(armed))

    # ---- AI lifecycle ---------------------------------------------------

    def on_transcription(self, text: str) -> None:
        self.reply_text.emit(text)
        self.state_requested.emit("thinking")

    def on_routing_started(self) -> None:
        self.state_requested.emit("routing_model")

    def on_stream_started(self) -> None:
        self._stream_buffer = ""
        self.state_requested.emit("streaming")

    def on_token(self, token: str) -> None:
        """Progressive display: the reply grows as tokens actually arrive."""
        self._stream_buffer += token
        self.stream_token.emit(self._stream_buffer)

    def on_stream_finished(self, text: str | None = None) -> None:
        if text:
            self._stream_buffer = text
        self.reply_text.emit(self._stream_buffer)

    def on_tool_started(self) -> None:
        self.state_requested.emit("executing_tool")

    def on_task_finished(self, ok: bool) -> None:
        self.state_requested.emit("success" if ok else "error")

    # ---- speech ---------------------------------------------------------

    def on_tts_started(self, current_state: str = "idle") -> None:
        """Speaking begins exactly when TTS does, not on a guess."""
        self._speaking_return_state = current_state
        self.state_requested.emit("speaking")

    def on_tts_level(self, level: float) -> None:
        self.tts_level.emit(max(0.0, min(1.0, float(level))))

    def on_tts_finished(self) -> None:
        """Stop speaking immediately; never leave FRIDAY visually talking."""
        self.tts_level.emit(0.0)
        self.state_requested.emit(self._speaking_return_state or "idle")

    def on_tts_cancelled(self) -> None:
        self.tts_level.emit(0.0)
        self.state_requested.emit("idle")

    # ---- connection -----------------------------------------------------

    def on_backend_disconnected(self) -> None:
        self.connection.emit(False)
        self.state_requested.emit("reconnecting")

    def on_backend_reconnected(self) -> None:
        self.connection.emit(True)
        self.state_requested.emit("idle")

    # ---- generic runtime label -----------------------------------------

    @pyqtSlot(str)
    def on_runtime_state(self, label: str) -> None:
        state = RUNTIME_STATES.get(str(label).upper())
        if state is None:
            log.debug("FRIDAY: ignoring unmapped runtime state %r", label)
            return
        self.state_requested.emit(state)


def connect(bridge: FridayBridge, window) -> None:
    """Attach a bridge to a :class:`~friday.window.FridayWindow`."""
    bridge.state_requested.connect(window.apply_state)
    bridge.mic_level.connect(window.push_mic_level)
    bridge.tts_level.connect(window.push_tts_level)
    bridge.vad_probability.connect(window.voice_panel.set_vad)
    bridge.wake_word_armed.connect(window.voice_panel.set_wake_word)
    bridge.muted.connect(window.voice_panel.set_muted)
    bridge.reply_text.connect(window.set_reply)
    bridge.stream_token.connect(window.set_reply)
    bridge.connection.connect(window.stage.avatar.set_connected)
    bridge.present_window.connect(window.present)
