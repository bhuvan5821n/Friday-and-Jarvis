from __future__ import annotations

from PyQt6.QtCore import QObject, pyqtSignal


class FridayStateController(QObject):
    """Normalizes backend lifecycle labels into visible FRIDAY states."""

    state_changed = pyqtSignal(str)

    _RUNTIME_MAP = {
        "INITIALISING": "idle", "MUTED": "sleeping", "LISTENING": "listening",
        "PROCESSING": "processing_speech", "THINKING": "thinking",
        "SPEAKING": "speaking", "RECONNECTING": "reconnecting",
        "ERROR": "error", "WARNING": "warning", "STREAMING": "streaming",
    }

    def __init__(self, parent=None):
        super().__init__(parent)
        self._state = "idle"

    @property
    def state(self) -> str:
        return self._state

    def update_from_runtime(self, runtime_state: str) -> str:
        state = self._RUNTIME_MAP.get(str(runtime_state).upper(), "idle")
        if state != self._state:
            self._state = state
            self.state_changed.emit(state)
        return state
