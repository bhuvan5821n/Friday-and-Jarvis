from __future__ import annotations

from dataclasses import dataclass

from PyQt6.QtCore import QObject, pyqtSignal


@dataclass(frozen=True)
class FridayModeProfile:
    name: str
    ring_speed: float
    particle_density: float
    face_intensity: float
    diagnostic_emphasis: bool
    status_label: str
    routing_preference: str


PROFILES = {
    "normal": FridayModeProfile(
        "normal", ring_speed=1.0, particle_density=0.35, face_intensity=0.72,
        diagnostic_emphasis=False, status_label="FRIDAY NORMAL MODE — READY",
        routing_preference="balanced",
    ),
    "battle": FridayModeProfile(
        "battle", ring_speed=2.25, particle_density=0.78, face_intensity=1.0,
        diagnostic_emphasis=True,
        status_label="BATTLE MODE — TACTICAL OVERRIDE ACTIVE",
        routing_preference="high_priority",
    ),
}


class FridayModeManager(QObject):
    """One FRIDAY component tree, configured by a mode profile.

    Runtime callers must request a concrete mode; text classification alone is
    intentionally not part of this manager, so phrases such as 'urgent' cannot
    silently enter Battle Mode.
    """

    mode_changed = pyqtSignal(object)

    def __init__(self, initial: str = "normal", parent=None):
        super().__init__(parent)
        self._mode = initial if initial in PROFILES else "normal"

    @property
    def mode(self) -> str:
        return self._mode

    @property
    def profile(self) -> FridayModeProfile:
        return PROFILES[self._mode]

    def set_mode(self, mode: str, *, explicit: bool = True) -> bool:
        """Switch only for an explicit/manual or verified system request."""
        if not explicit or mode not in PROFILES or mode == self._mode:
            return False
        self._mode = mode
        self.mode_changed.emit(self.profile)
        return True
