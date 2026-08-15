"""FRIDAY's window: top bar, three columns, avatar stage, dock.

This is the only FRIDAY window.  States change what it *shows* — colour, status,
which clip plays, which panel is emphasised — but never which window you are
looking at.  There is no separate battle dashboard and no per-emotion window.
"""
from __future__ import annotations

import logging
from datetime import datetime

from PyQt6.QtCore import QRectF, Qt, QTimer, pyqtSignal, pyqtSlot
from PyQt6.QtGui import QColor, QFont, QKeySequence, QPainter, QShortcut
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QLineEdit,
                             QMainWindow, QPushButton, QScrollArea, QSizePolicy,
                             QVBoxLayout, QWidget)

from . import data
from . import theme as T
from .avatar import FridayAvatar
from .panels import (AIControlPanel, ClipboardPanel, MemoryPanel, NetworkPanel,
                     RecentTasksPanel, RemindersPanel, SystemOverviewPanel,
                     VoiceStatusPanel, WorkspacePanel)
from .states import DEFAULT_STATE, visual_for
from .widgets import IconButton, ui_font

log = logging.getLogger("friday.window")

#: Dock tiles. The glyph is drawn from the UI font — no icon assets are
#: invented, and each tile is disabled unless its studio is really reachable.
DOCK_ITEMS = [
    ("Chat", "chat", "💬"), ("Image Studio", "images", "🖼"),
    ("Video Studio", "video", "▶"), ("Music Studio", "music", "♪"),
    ("Voice Studio", "voice", "🎙"), ("Code Studio", "code", "⌘"),
    ("Documents", "documents", "📄"), ("Research", "research", "🔍"),
    ("Automation", "automation", "⚙"), ("Settings", "settings", "⚙"),
]


class TopBar(QWidget):
    """Identity, clock, connection and current model — all live."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(T.TOPBAR_H)
        self.setStyleSheet(
            f"background:{T.BG_PANEL}; border-bottom:1px solid {T.BORDER_SUBTLE};")

        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(22)

        brand = QLabel("FRIDAY")
        brand.setFont(ui_font(20, QFont.Weight.Bold))
        brand.setStyleSheet(
            f"color:{T.TEXT}; letter-spacing:3px; background:transparent;"
            "border:none;")
        lay.addWidget(brand)

        self.subtitle = QLabel("Intelligence System")
        self.subtitle.setFont(ui_font(T.FS_MICRO))
        self.subtitle.setStyleSheet(
            f"color:{T.TEXT_FAINT}; background:transparent; border:none;")
        lay.addWidget(self.subtitle)
        lay.addSpacing(10)

        self.clock = self._stat("—", T.TEXT)
        self.date = self._stat("", T.TEXT_FAINT, micro=True)
        clock_box = QVBoxLayout()
        clock_box.setSpacing(0)
        clock_box.addWidget(self.clock)
        clock_box.addWidget(self.date)
        lay.addLayout(clock_box)

        lay.addStretch(1)

        self.connection = self._stat("Checking…", T.TEXT_FAINT)
        self.model = self._stat("No model yet", T.TEXT_DIM)
        self.voice = self._stat("READY", T.CYAN)
        for label, widget in (("CONNECTION", self.connection),
                              ("MODEL", self.model),
                              ("VOICE", self.voice)):
            lay.addLayout(self._labelled(label, widget))

        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        self._tick()

    def _stat(self, text: str, colour: str, micro: bool = False) -> QLabel:
        lbl = QLabel(text)
        lbl.setFont(ui_font(T.FS_MICRO if micro else T.FS_BODY,
                            QFont.Weight.Normal if micro else QFont.Weight.DemiBold))
        lbl.setStyleSheet(f"color:{colour}; background:transparent; border:none;")
        return lbl

    def _labelled(self, caption: str, widget: QLabel) -> QVBoxLayout:
        box = QVBoxLayout()
        box.setSpacing(1)
        cap = QLabel(caption)
        cap.setFont(ui_font(T.FS_MICRO))
        cap.setStyleSheet(
            f"color:{T.TEXT_MUTED}; letter-spacing:1.3px; background:transparent;"
            "border:none;")
        box.addWidget(cap)
        box.addWidget(widget)
        return box

    def _tick(self) -> None:
        now = datetime.now()
        self.clock.setText(now.strftime("%I:%M %p").lstrip("0"))
        self.date.setText(now.strftime("%A, %d %B %Y"))

        r = data.system.snapshot()
        if r.net_online is None:
            self.connection.setText("Checking…")
            self.connection.setStyleSheet(
                f"color:{T.TEXT_FAINT}; background:transparent; border:none;")
        else:
            online = r.net_online
            self.connection.setText("Online" if online else "Offline")
            self.connection.setStyleSheet(
                f"color:{T.GREEN if online else T.AMBER};"
                "background:transparent; border:none;")

        ai = data.ai_reading()
        name = ai.served or ai.model
        self.model.setText(name.split("/")[-1] if name else "No model yet")
        self.model.setToolTip(f"{ai.provider or 'provider unknown'} · {ai.routing}")

    def set_voice_state(self, label: str, colour: str) -> None:
        self.voice.setText(label)
        self.voice.setStyleSheet(
            f"color:{colour}; background:transparent; border:none;")


class DockTile(QPushButton):
    """A launcher tile: glyph above, label below, like the reference dock."""

    def __init__(self, label: str, glyph: str, parent: QWidget | None = None):
        super().__init__(parent)
        self._label = label
        self._glyph = glyph
        self.setFixedSize(104, 66)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self.setToolTip(f"Open {label}")
        self.setStyleSheet(f"""
            QPushButton {{ background:{T.BG_ELEVATED};
                border:1px solid {T.BORDER_SUBTLE}; border-radius:{T.RADIUS}px; }}
            QPushButton:hover {{ border-color:{T.BORDER_STRONG};
                background:rgba(0,140,205,55); }}
            QPushButton:pressed {{ background:rgba(0,170,235,100); }}
            QPushButton:focus {{ border-color:{T.CYAN}; }}
            QPushButton:disabled {{ background:rgba(12,20,32,140);
                border-color:{T.BORDER_SUBTLE}; }}
        """)

    def text(self) -> str:
        return self._label

    def paintEvent(self, event):
        super().paintEvent(event)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        enabled = self.isEnabled()

        p.setFont(ui_font(17))
        p.setPen(QColor(T.CYAN if enabled else T.TEXT_MUTED))
        p.drawText(QRectF(0, 9, self.width(), 24),
                   int(Qt.AlignmentFlag.AlignCenter), self._glyph)

        p.setFont(ui_font(T.FS_LABEL, QFont.Weight.DemiBold))
        p.setPen(QColor(T.TEXT if enabled else T.TEXT_MUTED))
        p.drawText(QRectF(2, 36, self.width() - 4, 22),
                   int(Qt.AlignmentFlag.AlignCenter), self._label)
        p.end()


class Dock(QWidget):
    """Bottom launcher. Every tile reports whether it is actually wired up."""

    activated = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(T.DOCK_H)
        self.setStyleSheet(
            f"background:{T.BG_PANEL}; border-top:1px solid {T.BORDER_SUBTLE};")
        lay = QHBoxLayout(self)
        lay.setContentsMargins(16, 12, 16, 12)
        lay.setSpacing(8)
        lay.addStretch(1)

        self.buttons: dict[str, DockTile] = {}
        for label, key, glyph in DOCK_ITEMS:
            tile = DockTile(label, glyph, self)
            tile.clicked.connect(lambda _=False, k=key: self.activated.emit(k))
            self.buttons[key] = tile
            lay.addWidget(tile)
        lay.addStretch(1)

    def set_available(self, key: str, available: bool, reason: str = "") -> None:
        """Disable a tile rather than let it fail silently when clicked."""
        tile = self.buttons.get(key)
        if tile is None:
            return
        tile.setEnabled(available)
        tile.setToolTip(reason if not available else f"Open {tile.text()}")


class CenterStage(QWidget):
    """The avatar, its status badge, the streamed reply, and the input row."""

    submitted = pyqtSignal(str)
    mic_clicked = pyqtSignal()
    attach_clicked = pyqtSignal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 6, 6, 6)
        lay.setSpacing(10)

        self.avatar = FridayAvatar(self)
        self.avatar.setSizePolicy(QSizePolicy.Policy.Expanding,
                                  QSizePolicy.Policy.Expanding)
        lay.addWidget(self.avatar, 1)

        self.badge = QLabel("READY")
        self.badge.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.badge.setFont(ui_font(T.FS_LABEL, QFont.Weight.Bold))
        self.badge.setStyleSheet(
            f"color:{T.CYAN}; letter-spacing:2.4px; background:transparent;")
        lay.addWidget(self.badge)

        self.reply = QLabel("How can I help you today?")
        self.reply.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.reply.setWordWrap(True)
        self.reply.setFont(ui_font(15))
        self.reply.setStyleSheet(f"color:{T.TEXT}; background:transparent;")
        self.reply.setMinimumHeight(52)
        self.reply.setMaximumHeight(96)
        lay.addWidget(self.reply)

        row = QFrame(self)
        row.setFixedHeight(50)
        row.setStyleSheet(
            f"background:{T.BG_INPUT}; border:1px solid {T.BORDER_STRONG};"
            f"border-radius:25px;")
        row_lay = QHBoxLayout(row)
        row_lay.setContentsMargins(18, 4, 8, 4)
        row_lay.setSpacing(8)

        self.input = QLineEdit(row)
        self.input.setPlaceholderText("Ask FRIDAY anything…")
        self.input.setFont(ui_font(T.FS_TITLE))
        self.input.setStyleSheet(
            f"QLineEdit {{ color:{T.TEXT}; background:transparent; border:none; }}"
            f"QLineEdit::placeholder {{ color:{T.TEXT_MUTED}; }}")
        self.input.returnPressed.connect(self._submit)
        row_lay.addWidget(self.input, 1)

        self.attach_btn = IconButton("Attach", "Attach an image, document or code file", row)
        self.attach_btn.clicked.connect(self.attach_clicked.emit)
        self.mic_btn = IconButton("Mic", "Sleep or wake FRIDAY's microphone", row,
                                  checkable=True)
        self.mic_btn.clicked.connect(self.mic_clicked.emit)
        self.send_btn = IconButton("Send", "Send this message", row)
        self.send_btn.clicked.connect(self._submit)
        for btn in (self.attach_btn, self.mic_btn, self.send_btn):
            row_lay.addWidget(btn)
        lay.addWidget(row)

    def _submit(self) -> None:
        text = self.input.text().strip()
        if not text:
            return
        self.input.clear()
        self.submitted.emit(text)

    def set_badge(self, label: str, colour: str) -> None:
        self.badge.setText(label)
        self.badge.setStyleSheet(
            f"color:{colour}; letter-spacing:2.4px; background:transparent;")


def _column(width: int) -> tuple[QScrollArea, QVBoxLayout]:
    """A fixed-width scrollable column, so panels never squash on short screens."""
    holder = QWidget()
    # Without an explicit cap the panels adopt their children's size hints (a
    # combo box full of long device names is enough) and overflow the viewport,
    # which silently clips every right-aligned value.
    holder.setFixedWidth(width - 10)
    lay = QVBoxLayout(holder)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(T.GAP)
    holder.setStyleSheet("background:transparent;")

    area = QScrollArea()
    area.setWidget(holder)
    area.setWidgetResizable(True)
    area.setFixedWidth(width)
    area.setFrameShape(QFrame.Shape.NoFrame)
    area.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
    area.setStyleSheet(f"""
        QScrollArea {{ background:transparent; border:none; }}
        QScrollBar:vertical {{ background:transparent; width:6px; margin:0; }}
        QScrollBar::handle:vertical {{ background:{T.BORDER}; border-radius:3px; }}
        QScrollBar::add-line, QScrollBar::sub-line {{ height:0; }}
    """)
    return area, lay


class FridayInterface(QWidget):
    """The canonical FRIDAY interface, as an embeddable widget.

    It is a widget rather than a window so it can mount inside the existing
    application shell — which is what guarantees there is only ever one FRIDAY
    window, with the established tray and hide behaviour preserved.
    """

    state_changed = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setStyleSheet(f"background:{T.BG_DEEP};")

        self._state = DEFAULT_STATE
        self._revert_timer = QTimer(self)
        self._revert_timer.setSingleShot(True)
        self._revert_timer.timeout.connect(self._revert_state)
        self._revert_to = DEFAULT_STATE

        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        outer.setSpacing(0)

        self.topbar = TopBar(self)
        outer.addWidget(self.topbar)

        body = QHBoxLayout()
        body.setContentsMargins(T.GAP, T.GAP, T.GAP, T.GAP)
        body.setSpacing(T.GAP)
        outer.addLayout(body, 1)

        left_area, left = _column(T.COL_LEFT)
        self.system_panel = SystemOverviewPanel()
        self.network_panel = NetworkPanel()
        self.voice_panel = VoiceStatusPanel()
        self.tasks_panel = RecentTasksPanel()
        for panel in (self.system_panel, self.network_panel,
                      self.voice_panel, self.tasks_panel):
            left.addWidget(panel)
        left.addStretch(1)
        body.addWidget(left_area)

        self.stage = CenterStage(self)
        body.addWidget(self.stage, 1)

        right_area, right = _column(T.COL_RIGHT)
        self.ai_panel = AIControlPanel()
        self.workspace_panel = WorkspacePanel()
        self.clipboard_panel = ClipboardPanel()
        self.reminders_panel = RemindersPanel()
        self.memory_panel = MemoryPanel()
        for panel in (self.ai_panel, self.workspace_panel, self.clipboard_panel,
                      self.reminders_panel, self.memory_panel):
            right.addWidget(panel)
        right.addStretch(1)
        body.addWidget(right_area)

        self.dock = Dock(self)
        outer.addWidget(self.dock)

        self.apply_state(DEFAULT_STATE)

    # ---- state ---------------------------------------------------------

    @property
    def state(self) -> str:
        return self._state

    @pyqtSlot(str)
    def apply_state(self, state: str) -> None:
        """Single entry point for every visible state change."""
        visual = visual_for(state)
        self._state = state

        self.stage.avatar.set_emotion(visual.emotion)
        self.stage.avatar.set_accent(visual.accent)
        self.stage.avatar.set_speaking(state == "speaking")
        self.stage.set_badge(visual.status, visual.accent)
        self.topbar.set_voice_state(visual.status, visual.accent)
        self.voice_panel.set_state(state, visual.status, visual.accent)
        self.state_changed.emit(state)

        # Momentary states return on their own rather than sticking.
        if visual.hold_ms:
            self._revert_to = visual.revert_to or DEFAULT_STATE
            self._revert_timer.start(visual.hold_ms)
        else:
            self._revert_timer.stop()

    def _revert_state(self) -> None:
        self.apply_state(self._revert_to)

    # ---- live signals --------------------------------------------------

    def push_mic_level(self, level: float) -> None:
        self.voice_panel.push_level(level)
        self.stage.avatar.set_microphone_level(level)

    def push_tts_level(self, level: float) -> None:
        self.stage.avatar.set_tts_level(level)

    def set_reply(self, text: str) -> None:
        self.stage.reply.setText(text)

    def set_paused(self, paused: bool) -> None:
        """Suspend avatar decoding while FRIDAY is hidden or minimized."""
        self.stage.avatar.set_paused(paused)

    def present(self) -> None:
        """Focus the input. Window-level raising belongs to the host shell."""
        self.stage.input.setFocus()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.stage.avatar.set_paused(True)

    def showEvent(self, event):
        super().showEvent(event)
        self.stage.avatar.set_paused(False)


class FridayWindow(QMainWindow):
    """Standalone host for :class:`FridayInterface` (development and testing).

    The shipped application embeds ``FridayInterface`` in its existing window;
    this wrapper exists so the interface can be run and screenshotted alone.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle("FRIDAY")
        self.setMinimumSize(1180, 740)
        self.resize(1600, 940)
        self.setStyleSheet(f"QMainWindow {{ background:{T.BG_DEEP}; }}")

        self.ui = FridayInterface(self)
        self.setCentralWidget(self.ui)

        QShortcut(QKeySequence("Ctrl+L"), self,
                  activated=self.ui.stage.input.setFocus)

    # Delegate the interface surface so existing tests keep working.
    def __getattr__(self, name):
        # Only consulted for attributes QMainWindow does not define.
        return getattr(self.ui, name)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == event.Type.WindowStateChange:
            self.ui.set_paused(self.isMinimized())

    def present(self) -> None:
        """Restore, raise and focus the one window. Never opens a second."""
        if self.isMinimized():
            self.showNormal()
        elif not self.isVisible():
            self.show()
        self.raise_()
        self.activateWindow()
        self.ui.stage.input.setFocus()
