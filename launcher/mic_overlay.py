"""Compact microphone overlay — Ctrl+Alt+M's face.

A small always-on-top-while-listening pill that shows which assistant is
active, whether the microphone is live, and the *actual* input amplitude fed
from the same RMS stream the assistant already publishes.  It owns no audio
device itself — it is a view onto the existing pipeline, so opening it cannot
fight the assistant for the microphone.

Lives inside the assistant process; OPEN_MIC/CLOSE_MIC show and hide it.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QLinearGradient, QPainter, QPen
from PyQt6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QWidget

log = logging.getLogger("lifecycle.overlay")

_POS_FILE = Path(__file__).resolve().parent.parent / "runtime" / "mic_overlay_pos.json"

CYAN = "#00d4ff"
BG = "#0a1020"
TEXT = "#e8f4ff"
MUTEDC = "#f0a832"


class MicOverlay(QWidget):
    """Frameless draggable pill: name, live waveform, state, mute button."""

    mute_requested = pyqtSignal()
    close_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.Tool)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setFixedSize(340, 74)

        self._assistant = "FRIDAY"
        self._state = "LISTENING"
        self._muted = False
        self._levels = [0.0] * 48
        self._drag_at: QPointF | None = None

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 8, 10, 8)
        lay.setSpacing(10)
        lay.addStretch(1)

        self._mute_btn = QPushButton("Mute", self)
        self._mute_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mute_btn.setFixedSize(56, 26)
        self._mute_btn.setStyleSheet(
            f"QPushButton {{ color:{TEXT}; background:rgba(0,140,205,60);"
            f" border:1px solid rgba(0,212,255,110); border-radius:13px;"
            f" font-size:11px; }}"
            "QPushButton:hover { background:rgba(0,170,235,110); }")
        self._mute_btn.clicked.connect(self.mute_requested.emit)
        lay.addWidget(self._mute_btn, 0, Qt.AlignmentFlag.AlignBottom)

        close_btn = QPushButton("×", self)
        close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        close_btn.setFixedSize(26, 26)
        close_btn.setToolTip("Hide the microphone overlay (keeps listening)")
        close_btn.setStyleSheet(
            f"QPushButton {{ color:{TEXT}; background:transparent;"
            f" border:1px solid rgba(120,160,200,70); border-radius:13px; }}"
            "QPushButton:hover { border-color:#00d4ff; }")
        close_btn.clicked.connect(self.close_requested.emit)
        lay.addWidget(close_btn, 0, Qt.AlignmentFlag.AlignTop)

        self._restore_position()

    # ---- data in ---------------------------------------------------------

    def set_assistant(self, name: str) -> None:
        self._assistant = name.upper()
        self.update()

    def set_state(self, state: str, muted: bool) -> None:
        self._state = state
        self._muted = muted
        self._mute_btn.setText("Unmute" if muted else "Mute")
        # Always-on-top only while actively listening, per spec.
        listening = not muted
        flags = self.windowFlags()
        want = flags | Qt.WindowType.WindowStaysOnTopHint if listening \
            else flags & ~Qt.WindowType.WindowStaysOnTopHint
        if want != flags:
            visible = self.isVisible()
            self.setWindowFlags(want)
            if visible:
                self.show()
        if muted:
            self._levels = [0.0] * len(self._levels)
        self.update()

    def push_level(self, level: float) -> None:
        if self._muted:
            level = 0.0
        self._levels = self._levels[1:] + [max(0.0, min(1.0, level))]
        self.update()

    # ---- painting ----------------------------------------------------------

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        r = QRectF(self.rect()).adjusted(1, 1, -1, -1)

        p.setBrush(QColor(BG))
        accent = QColor(MUTEDC if self._muted else CYAN)
        pen = QPen(accent)
        pen.setWidthF(1.2)
        p.setPen(pen)
        p.drawRoundedRect(r, 20, 20)

        p.setPen(QColor(TEXT))
        p.setFont(QFont("Segoe UI", 10, QFont.Weight.Bold))
        p.drawText(QRectF(18, 8, 160, 18), int(Qt.AlignmentFlag.AlignLeft),
                   self._assistant)

        p.setPen(accent)
        p.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        status = "MUTED" if self._muted else self._state
        p.drawText(QRectF(18, 48, 200, 14), int(Qt.AlignmentFlag.AlignLeft), status)

        # Waveform strip — real amplitudes only; silent input draws flat.
        wave = QRectF(18, 27, 200, 20)
        mid = wave.center().y()
        grad = QLinearGradient(wave.topLeft(), wave.topRight())
        grad.setColorAt(0.0, QColor(0, 212, 255, 60))
        grad.setColorAt(1.0, accent)
        pen = QPen(accent)
        pen.setWidthF(2.0)
        p.setPen(pen)
        n = len(self._levels)
        step = wave.width() / max(1, n - 1)
        for i, level in enumerate(self._levels):
            x = wave.left() + i * step
            h = max(0.6, level * (wave.height() / 2))
            p.drawLine(QPointF(x, mid - h), QPointF(x, mid + h))
        p.end()

    # ---- dragging + remembered position ------------------------------------

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._drag_at = e.globalPosition() - QPointF(self.frameGeometry().topLeft())

    def mouseMoveEvent(self, e):
        if self._drag_at is not None:
            self.move((e.globalPosition() - self._drag_at).toPoint())

    def mouseReleaseEvent(self, _e):
        if self._drag_at is not None:
            self._drag_at = None
            self._save_position()

    def _save_position(self) -> None:
        try:
            _POS_FILE.parent.mkdir(parents=True, exist_ok=True)
            _POS_FILE.write_text(json.dumps(
                {"x": self.x(), "y": self.y()}), encoding="utf-8")
        except Exception as exc:
            log.debug("could not save overlay position: %s", exc)

    def _restore_position(self) -> None:
        try:
            pos = json.loads(_POS_FILE.read_text(encoding="utf-8"))
            self.move(int(pos["x"]), int(pos["y"]))
        except Exception:
            # First run: bottom-right of the primary screen.
            from PyQt6.QtWidgets import QApplication
            screen = QApplication.primaryScreen()
            if screen is not None:
                geo = screen.availableGeometry()
                self.move(geo.right() - self.width() - 24,
                          geo.bottom() - self.height() - 48)
