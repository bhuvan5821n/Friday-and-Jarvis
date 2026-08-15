"""Shared building blocks for FRIDAY's panels.

Every widget here has a first-class "no data" appearance.  A metric that has
not reported yet renders as a dim placeholder rather than a zero, because a
confident-looking 0% is a lie about the machine's state.
"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt, pyqtSignal
from PyQt6.QtGui import QColor, QFont, QPainter, QPen
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QSizePolicy, QVBoxLayout, QWidget)

from . import theme as T


def ui_font(size: int = T.FS_BODY, weight: QFont.Weight = QFont.Weight.Normal) -> QFont:
    f = QFont(T.FONT_UI.split(",")[0], size)
    f.setWeight(weight)
    return f


def mono_font(size: int = T.FS_LABEL) -> QFont:
    return QFont(T.FONT_MONO.split(",")[0], size)


class Panel(QFrame):
    """A glass surface with a title row and a content area.

    ``collapsible`` panels remember nothing across runs by design: the layout
    should look the same every time FRIDAY opens.
    """

    def __init__(self, title: str, parent: QWidget | None = None,
                 accent: str = T.CYAN, collapsible: bool = False):
        super().__init__(parent)
        self._accent = accent
        self.setStyleSheet(f"QFrame {{ {T.panel_qss()} }}")

        outer = QVBoxLayout(self)
        outer.setContentsMargins(T.PAD, 10, T.PAD, 11)
        outer.setSpacing(7)

        header = QHBoxLayout()
        header.setSpacing(8)
        self._title = QLabel(title.upper())
        self._title.setFont(ui_font(T.FS_LABEL, QFont.Weight.DemiBold))
        self._title.setStyleSheet(
            f"color:{T.TEXT_DIM}; letter-spacing:1.4px; background:transparent;"
            "border:none;")
        header.addWidget(self._title)
        header.addStretch(1)

        self._status = QLabel()
        self._status.setFont(ui_font(T.FS_MICRO))
        self._status.setStyleSheet(
            f"color:{T.TEXT_FAINT}; background:transparent; border:none;")
        header.addWidget(self._status)
        outer.addLayout(header)

        self.body = QWidget(self)
        self.body.setStyleSheet("background:transparent; border:none;")
        self._body_layout = QVBoxLayout(self.body)
        self._body_layout.setContentsMargins(0, 0, 0, 0)
        self._body_layout.setSpacing(8)
        outer.addWidget(self.body)

        if collapsible:
            self._title.setCursor(Qt.CursorShape.PointingHandCursor)

    def content(self) -> QVBoxLayout:
        return self._body_layout

    def set_status(self, text: str, color: str | None = None) -> None:
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color:{color or T.TEXT_FAINT}; background:transparent; border:none;")

    def set_accent(self, color: str) -> None:
        self._accent = color
        self.setStyleSheet(f"QFrame {{ {T.panel_qss(border=color)} }}")


class MetricRow(QWidget):
    """One system metric: label, value, and a fill bar.

    Until :meth:`set_value` receives a real reading the row shows an em dash and
    an empty track, never a fabricated number.
    """

    def __init__(self, label: str, sub: str = "", accent: str = T.CYAN,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._accent = QColor(accent)
        self._value: float | None = None
        self._text = "—"
        self._label = label
        self._sub = sub
        self.setFixedHeight(40)
        self.setStyleSheet("background:transparent; border:none;")

    def set_value(self, pct: float | None, text: str | None = None) -> None:
        self._value = None if pct is None else max(0.0, min(100.0, float(pct)))
        if text is not None:
            self._text = text
        elif pct is None:
            self._text = "—"
        else:
            self._text = f"{pct:.0f}%"
        self.update()

    def set_sub(self, sub: str) -> None:
        self._sub = sub
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()

        p.setFont(ui_font(T.FS_BODY, QFont.Weight.DemiBold))
        p.setPen(QColor(T.TEXT))
        p.drawText(QRectF(0, 1, w * 0.6, 15),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self._label)

        if self._sub:
            p.setFont(ui_font(T.FS_MICRO))
            p.setPen(QColor(T.TEXT_FAINT))
            p.drawText(QRectF(0, 15, w * 0.66, 13),
                       int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                       self._sub)

        p.setFont(mono_font(T.FS_BODY))
        p.setPen(QColor(T.TEXT if self._value is not None else T.TEXT_MUTED))
        p.drawText(QRectF(w * 0.6, 1, w * 0.4, 15),
                   int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                   self._text)

        track = QRectF(0, h - 7, w, 3)
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(QColor(40, 62, 88, 130))
        p.drawRoundedRect(track, 1.5, 1.5)
        if self._value:
            fill = QRectF(track)
            fill.setWidth(track.width() * self._value / 100.0)
            p.setBrush(self._accent)
            p.drawRoundedRect(fill, 1.5, 1.5)
        p.end()


class Waveform(QWidget):
    """Scrolling amplitude history driven exclusively by real audio levels.

    ``push`` is called by whatever owns the audio stream.  Nothing in this
    widget generates motion on its own, so a silent microphone draws a flat
    line — which is the honest picture.
    """

    def __init__(self, parent: QWidget | None = None, accent: str = T.CYAN,
                 bars: int = 46):
        super().__init__(parent)
        self._bars = bars
        self._levels = [0.0] * bars
        self._accent = QColor(accent)
        self._active = True
        self.setMinimumHeight(38)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Preferred)
        self.setStyleSheet("background:transparent; border:none;")

    def push(self, level: float) -> None:
        self._levels.append(max(0.0, min(1.0, float(level))))
        del self._levels[:-self._bars]
        self.update()

    def set_accent(self, color: str) -> None:
        self._accent = QColor(color)
        self.update()

    def set_active(self, active: bool) -> None:
        """When inactive (muted) the trace is drawn flat and dim."""
        self._active = bool(active)
        self.update()

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        mid = h / 2.0
        n = len(self._levels)
        if n == 0 or w <= 0:
            p.end()
            return
        bar_w = max(1.5, w / n * 0.62)
        step = w / n
        for i, lv in enumerate(self._levels):
            amp = (lv if self._active else 0.0) * (h * 0.44)
            amp = max(amp, 0.8)
            col = QColor(self._accent if self._active else QColor(T.TEXT_MUTED))
            # Recent samples read brighter, so the eye follows the live edge.
            col.setAlpha(int(70 + 150 * (i / n)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(col)
            p.drawRoundedRect(QRectF(i * step, mid - amp, bar_w, amp * 2), 1.0, 1.0)
        p.end()


class IconButton(QPushButton):
    """A control that is either genuinely wired up or visibly disabled."""

    def __init__(self, text: str, tooltip: str = "", parent: QWidget | None = None,
                 accent: str = T.CYAN, checkable: bool = False):
        super().__init__(text, parent)
        self._accent = accent
        self.setCheckable(checkable)
        self.setFont(ui_font(T.FS_LABEL, QFont.Weight.DemiBold))
        if tooltip:
            self.setToolTip(tooltip)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._restyle()

    def _restyle(self) -> None:
        self.setStyleSheet(f"""
            QPushButton {{
                color:{T.TEXT}; background:{T.BG_ELEVATED};
                border:1px solid {T.BORDER}; border-radius:{T.RADIUS_SM}px;
                padding:7px 12px;
            }}
            QPushButton:hover {{ border-color:{self._accent};
                background:rgba(0,140,205,55); }}
            QPushButton:pressed {{ background:rgba(0,170,235,105); }}
            QPushButton:checked {{ color:#04121e; background:{self._accent};
                border-color:{self._accent}; }}
            QPushButton:focus {{ border-color:{self._accent}; }}
            QPushButton:disabled {{ color:{T.TEXT_MUTED};
                background:rgba(12,20,32,150);
                border-color:{T.BORDER_SUBTLE}; }}
        """)

    def setEnabled(self, enabled: bool) -> None:  # noqa: N802 (Qt naming)
        super().setEnabled(enabled)
        self.setCursor(Qt.CursorShape.PointingHandCursor if enabled
                       else Qt.CursorShape.ForbiddenCursor)


class StatRow(QWidget):
    """A key/value line used by the text-heavy panels."""

    def __init__(self, key: str, value: str = "—", parent: QWidget | None = None,
                 mono_value: bool = False):
        super().__init__(parent)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(8)
        self.setStyleSheet("background:transparent; border:none;")

        k = QLabel(key)
        k.setFont(ui_font(T.FS_LABEL))
        k.setStyleSheet(f"color:{T.TEXT_FAINT}; background:transparent;")
        lay.addWidget(k)
        lay.addStretch(1)

        self._value = QLabel(value)
        self._value.setFont(mono_font(T.FS_LABEL) if mono_value else
                            ui_font(T.FS_LABEL, QFont.Weight.DemiBold))
        self._value.setStyleSheet(f"color:{T.TEXT_MUTED}; background:transparent;")
        self._value.setAlignment(Qt.AlignmentFlag.AlignRight)
        lay.addWidget(self._value)

    def set_value(self, text: str | None, color: str | None = None) -> None:
        """``None`` renders the honest placeholder rather than a stale value."""
        known = bool(text)
        self._value.setText(text or "Not available")
        self._value.setStyleSheet(
            f"color:{color or (T.TEXT if known else T.TEXT_MUTED)};"
            "background:transparent;")


class ListRow(QWidget):
    """One entry in Recent Tasks / Reminders, with a state dot."""

    clicked = pyqtSignal()

    def __init__(self, title: str, meta: str = "", dot: str = T.GREEN,
                 parent: QWidget | None = None):
        super().__init__(parent)
        self._dot = QColor(dot)
        self._title = title
        self._meta = meta
        self.setFixedHeight(34)
        self.setStyleSheet("background:transparent; border:none;")

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(self._dot)
        p.drawEllipse(QRectF(1, h / 2 - 3, 6, 6))

        p.setFont(ui_font(T.FS_LABEL))
        p.setPen(QColor(T.TEXT))
        p.drawText(QRectF(16, 0, w - 100, h),
                   int(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter),
                   self._title)

        if self._meta:
            p.setFont(ui_font(T.FS_MICRO))
            p.setPen(QColor(T.TEXT_FAINT))
            p.drawText(QRectF(w - 96, 0, 96, h),
                       int(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter),
                       self._meta)
        p.end()


class EmptyState(QLabel):
    """Says plainly why a panel has nothing to show."""

    def __init__(self, message: str, parent: QWidget | None = None):
        super().__init__(message, parent)
        self.setFont(ui_font(T.FS_LABEL))
        self.setWordWrap(True)
        self.setStyleSheet(
            f"color:{T.TEXT_MUTED}; background:transparent; border:none;"
            "padding:6px 0;")


def divider() -> QFrame:
    line = QFrame()
    line.setFixedHeight(1)
    line.setStyleSheet(f"background:{T.BORDER_SUBTLE}; border:none;")
    return line
