"""FRIDAY Avatar Lab — a developer harness for the video avatar.

Run it directly to verify, before any dashboard work, that every clip loads,
stays square, crossfades without a black frame, and never plays its own audio::

    .venv/Scripts/python.exe -m friday.avatar_lab

The simulated TTS and microphone levels here are explicitly labelled as
simulation: they exist to exercise the amplitude path, and are replaced by real
backend levels in the running interface.
"""
from __future__ import annotations

import math
import sys

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QApplication, QGridLayout, QHBoxLayout, QLabel,
                             QPushButton, QVBoxLayout, QWidget)

from .assets import library
from .avatar import FridayAvatar
from .states import visual_for

MONO = "Cascadia Mono, Consolas, monospace"
UI_FONT = "Segoe UI, Inter, sans-serif"


class AvatarLab(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("FRIDAY Avatar Lab")
        self.resize(1180, 780)
        self.setStyleSheet("background: #050a14;")

        self._lib = library()
        self._sim_tts = False
        self._sim_mic = False
        self._sim_phase = 0.0
        self._saved_emotion = "neutral"

        root = QHBoxLayout(self)
        root.setContentsMargins(18, 18, 18, 18)
        root.setSpacing(18)

        self.avatar = FridayAvatar(self)
        root.addWidget(self.avatar, 3)

        side = QVBoxLayout()
        side.setSpacing(10)
        root.addLayout(side, 2)

        self.readout = QLabel()
        self.readout.setFont(QFont(MONO.split(",")[0], 9))
        self.readout.setStyleSheet(
            "color:#7fe7ff; background:rgba(10,22,40,200);"
            "border:1px solid rgba(0,212,255,60); border-radius:8px; padding:12px;")
        self.readout.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.readout.setMinimumHeight(190)
        side.addWidget(self.readout)

        side.addWidget(self._heading("EMOTIONS"))
        grid = QGridLayout()
        grid.setSpacing(6)
        side.addLayout(grid)
        emotions = ["neutral", "listening", "thinking", "thinking_alt", "happy",
                    "curious", "surprised", "speaking", "speaking_emphasis"]
        for i, name in enumerate(emotions):
            available = self._lib.has(name)
            btn = self._button(self._lib.get(name).label if available else f"{name} (missing)",
                               enabled=available)
            btn.clicked.connect(lambda _=False, n=name: self.avatar.set_emotion(n))
            grid.addWidget(btn, i // 2, i % 2)

        side.addWidget(self._heading("SIMULATED EVENTS"))
        events = [
            ("Simulate TTS Start", self._tts_start),
            ("Simulate TTS Finish", self._tts_finish),
            ("Simulate Wake Word", self._wake_word),
            ("Simulate Error", self._error),
            ("Toggle Mic Input", self._toggle_mic),
            ("Toggle Reduced Motion", self._toggle_reduced),
        ]
        egrid = QGridLayout()
        egrid.setSpacing(6)
        side.addLayout(egrid)
        for i, (label, slot) in enumerate(events):
            btn = self._button(label)
            btn.clicked.connect(slot)
            egrid.addWidget(btn, i // 2, i % 2)

        side.addStretch(1)

        self._missing_label = QLabel()
        self._missing_label.setFont(QFont(MONO.split(",")[0], 8))
        self._missing_label.setWordWrap(True)
        self._missing_label.setStyleSheet("color:#ffb020;")
        missing = self._lib.missing
        self._missing_label.setText(
            "All manifest clips present." if not missing
            else "MISSING:\n" + "\n".join(missing))
        side.addWidget(self._missing_label)

        self._timer = QTimer(self)
        self._timer.setInterval(50)
        self._timer.timeout.connect(self._refresh)
        self._timer.start()

        self.avatar.media_error.connect(
            lambda msg: self._missing_label.setText(f"MEDIA ERROR: {msg}"))

    # ---- widgets -------------------------------------------------------

    def _heading(self, text: str) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(QFont(UI_FONT.split(",")[0], 8, QFont.Weight.DemiBold))
        lbl.setStyleSheet("color:#4a6a85; letter-spacing:2px; margin-top:6px;")
        return lbl

    def _button(self, text: str, enabled: bool = True) -> QPushButton:
        btn = QPushButton(text)
        btn.setEnabled(enabled)
        btn.setCursor(Qt.CursorShape.PointingHandCursor if enabled
                      else Qt.CursorShape.ForbiddenCursor)
        btn.setFont(QFont(UI_FONT.split(",")[0], 9))
        btn.setStyleSheet("""
            QPushButton { color:#cfe9ff; background:rgba(14,30,52,220);
                border:1px solid rgba(0,212,255,55); border-radius:7px;
                padding:8px 10px; text-align:left; }
            QPushButton:hover { background:rgba(0,120,190,90);
                border-color:rgba(0,212,255,140); }
            QPushButton:pressed { background:rgba(0,150,220,140); }
            QPushButton:disabled { color:#3f5468; border-color:rgba(80,100,120,45);
                background:rgba(12,20,32,160); }
        """)
        return btn

    # ---- simulated events ----------------------------------------------

    def _tts_start(self):
        self._saved_emotion = self.avatar.emotion
        self._sim_tts = True
        self.avatar.set_speaking(True)
        self.avatar.set_emotion("speaking")

    def _tts_finish(self):
        self._sim_tts = False
        self.avatar.set_speaking(False)
        self.avatar.set_tts_level(0.0)
        self.avatar.set_emotion(self._saved_emotion or "neutral")

    def _wake_word(self):
        self.avatar.set_emotion("listening")
        self._sim_mic = True

    def _error(self):
        self.avatar.set_accent(visual_for("error").accent)
        self.avatar.set_emotion("neutral")
        QTimer.singleShot(2200, lambda: self.avatar.set_accent(visual_for("idle").accent))

    def _toggle_mic(self):
        self._sim_mic = not self._sim_mic
        if not self._sim_mic:
            self.avatar.set_microphone_level(0.0)

    def _toggle_reduced(self):
        self._reduced = not getattr(self, "_reduced", False)
        self.avatar.set_reduced_motion(self._reduced)

    # ---- readout -------------------------------------------------------

    def _refresh(self):
        self._sim_phase += 0.08
        if self._sim_tts:
            level = 0.45 + 0.4 * abs(math.sin(self._sim_phase * 1.7))
            self.avatar.set_tts_level(level)
        if self._sim_mic:
            self.avatar.set_microphone_level(
                0.30 + 0.35 * abs(math.sin(self._sim_phase * 0.9)))

        clip = self.avatar.current_clip
        pos = self.avatar.playback_position()
        dur = self.avatar.playback_duration()
        self.readout.setText(
            f"emotion      {self.avatar.emotion}\n"
            f"previous     {self.avatar.previous_emotion}\n"
            f"file         {clip.path.name if clip else '—'}\n"
            f"loop         {clip.loop if clip else '—'}\n"
            f"media        {self.avatar.media_status()}\n"
            f"position     {pos:>6} / {dur} ms\n"
            f"tts level    {'█' * int(self.avatar._tts_level * 20):<20}\n"
            f"mic level    {'█' * int(self.avatar._mic_level * 20):<20}\n"
            f"clips        {len(self._lib.available)}/{len(self._lib)} available"
        )


def main() -> int:
    app = QApplication(sys.argv)
    lab = AvatarLab()
    lab.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
