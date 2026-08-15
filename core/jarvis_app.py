"""JarvisApp — Native OS assistant experience.

Manages the JARVIS interface as a background service with instant wake-word
response, smooth animations, and native Windows feel.

States:
    HIDDEN      → Running in memory, listening for wake word
    LISTENING   → Wake word detected, interface animating in
    THINKING    → Processing request, showing thinking animation
    CONVERSATION → Active conversation, accepting input
"""
from __future__ import annotations

import os
import sys
import time
import threading
from enum import Enum
from pathlib import Path
from typing import Callable, Optional

from PyQt6.QtCore import (
    QEasingCurve, QPropertyAnimation, QParallelAnimationGroup,
    QSequentialAnimationGroup, QRect, QPoint, QSize, Qt,
    QTimer, pyqtSignal, QObject, pyqtProperty,
)
from PyQt6.QtGui import (
    QColor, QKeySequence, QShortcut, QIcon, QPixmap,
    QPainter, QPainterPath, QBrush, QPen, QRadialGradient,
)
from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QSystemTrayIcon, QMenu,
    QWidget, QGraphicsOpacityEffect,
)


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()


class JarvisState(Enum):
    HIDDEN = "hidden"
    LISTENING = "listening"
    THINKING = "thinking"
    CONVERSATION = "conversation"


class JarvisWindow(QMainWindow):
    """Borderless, glassmorphism JARVIS window with smooth animations."""

    # Signals
    state_changed = pyqtSignal(str)
    wake_word_detected = pyqtSignal()
    conversation_ended = pyqtSignal()
    hotkey_pressed = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        
        # State
        self._state = JarvisState.HIDDEN
        self._last_monitor = None
        self._auto_hide_timer = QTimer(self)
        self._auto_hide_timer.setSingleShot(True)
        self._auto_hide_timer.timeout.connect(self._auto_hide)
        self._conversation_timeout = 30000  # 30 seconds default
        
        # Window setup
        self._setup_window()
        self._setup_animations()
        self._setup_hotkeys()
        self._setup_tray()
        
        # Start hidden
        self.hide()

    def _setup_window(self):
        """Configure borderless glassmorphism window."""
        self.setWindowFlags(
            Qt.WindowType.FramelessWindowHint |
            Qt.WindowType.WindowStaysOnTopHint |
            Qt.WindowType.Tool
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
        
        # Size
        self.setMinimumSize(820, 580)
        self.resize(980, 700)
        
        # Center on primary screen
        self._center_on_screen()

    def _setup_animations(self):
        """Configure smooth open/close animations."""
        # Opacity effect
        self._opacity_effect = QGraphicsOpacityEffect(self)
        self._opacity_effect.setOpacity(0.0)
        self.setGraphicsEffect(self._opacity_effect)
        
        # Fade animation
        self._fade_anim = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_anim.setDuration(250)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        
        # Position animation (slide up)
        self._pos_anim = QPropertyAnimation(self, b"pos")
        self._pos_anim.setDuration(250)
        self._pos_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        
        # Scale animation group
        self._open_group = QParallelAnimationGroup()
        self._open_group.addAnimation(self._fade_anim)
        self._open_group.addAnimation(self._pos_anim)
        
        # Close animation
        self._close_group = QParallelAnimationGroup()
        
        self._fade_close = QPropertyAnimation(self._opacity_effect, b"opacity")
        self._fade_close.setDuration(200)
        self._fade_close.setEasingCurve(QEasingCurve.Type.InCubic)
        
        self._pos_close = QPropertyAnimation(self, b"pos")
        self._pos_close.setDuration(200)
        self._pos_close.setEasingCurve(QEasingCurve.Type.InCubic)
        
        self._close_group.addAnimation(self._fade_close)
        self._close_group.addAnimation(self._pos_close)

    def _setup_hotkeys(self):
        """Register global hotkeys."""
        # Ctrl+Space — Open JARVIS
        self._shortcut_ctrl_space = QShortcut(QKeySequence("Ctrl+Space"), self)
        self._shortcut_ctrl_space.activated.connect(lambda: self.activate(JarvisState.LISTENING))
        
        # Alt+J — Open JARVIS
        self._shortcut_alt_j = QShortcut(QKeySequence("Alt+J"), self)
        self._shortcut_alt_j.activated.connect(lambda: self.activate(JarvisState.LISTENING))
        
        # Escape — Minimize
        self._shortcut_escape = QShortcut(QKeySequence("Escape"), self)
        self._shortcut_escape.activated.connect(self.hide_to_tray)

    def _setup_tray(self):
        """Create system tray with full menu."""
        self._tray = QSystemTrayIcon(self)
        self._tray.setToolTip("JARVIS — Say 'Jarvis' to wake")
        
        # Icon
        icon_path = BASE_DIR / "assets" / "jarvis.ico"
        if icon_path.exists():
            self._tray.setIcon(QIcon(str(icon_path)))
        else:
            self._tray.setIcon(self._create_icon())
        
        # Menu
        menu = QMenu()
        menu.setStyleSheet("""
            QMenu {
                background: #0a0d12;
                color: #8ffcff;
                border: 1px solid #1a5c7a;
                border-radius: 6px;
                padding: 4px;
            }
            QMenu::item {
                padding: 8px 24px;
                border-radius: 4px;
            }
            QMenu::item:selected {
                background: #001f2e;
                color: #00d4ff;
            }
            QMenu::separator {
                height: 1px;
                background: #0d3347;
                margin: 4px 8px;
            }
        """)
        
        # Actions
        open_action = menu.addAction("Open JARVIS")
        open_action.triggered.connect(lambda: self.activate(JarvisState.LISTENING))
        
        menu.addSeparator()
        
        self._mute_action = menu.addAction("Mute Microphone")
        self._mute_action.setCheckable(True)
        self._mute_action.triggered.connect(self._toggle_mute)
        
        menu.addSeparator()
        
        settings_action = menu.addAction("Settings")
        settings_action.triggered.connect(self._open_settings)
        
        restart_action = menu.addAction("Restart")
        restart_action.triggered.connect(self._restart)
        
        menu.addSeparator()
        
        exit_action = menu.addAction("Exit")
        exit_action.triggered.connect(self._exit)
        
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(self._tray_activated)
        self._tray.show()

    def _create_icon(self) -> QIcon:
        """Create a fallback arc-reactor icon."""
        pm = QPixmap(64, 64)
        pm.fill(Qt.GlobalColor.transparent)
        
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Background
        p.setBrush(QColor("#04141c"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        
        # Ring
        p.setPen(QPen(QColor("#00d4ff"), 5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(10, 10, 44, 44)
        
        # Core
        p.setBrush(QColor("#00d4ff"))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(26, 26, 12, 12)
        
        p.end()
        return QIcon(pm)

    def _center_on_screen(self):
        """Center window on the screen containing the cursor."""
        cursor_pos = QApplication.primaryScreen().geometry().center()
        
        # Try to find the monitor with cursor
        for screen in QApplication.screens():
            if screen.geometry().contains(cursor_pos):
                cursor_pos = screen.geometry().center()
                self._last_monitor = screen
                break
        
        x = cursor_pos.x() - self.width() // 2
        y = cursor_pos.y() - self.height() // 2
        self.move(x, y)

    def _tray_activated(self, reason):
        """Handle tray icon clicks."""
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.activate(JarvisState.LISTENING)

    def _toggle_mute(self):
        """Toggle microphone mute state."""
        muted = self._mute_action.isChecked()
        self._mute_action.setText("Unmute Microphone" if muted else "Mute Microphone")
        # Emit signal for main app to handle
        self.state_changed.emit("mute" if muted else "unmute")

    def _open_settings(self):
        """Open settings page."""
        self.activate(JarvisState.CONVERSATION)
        self.state_changed.emit("open_settings")

    def _restart(self):
        """Restart the application."""
        self._exit()
        import subprocess
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable])
        else:
            subprocess.Popen([sys.executable, str(BASE_DIR / "main.py")])
        os._exit(0)

    def _exit(self):
        """Clean exit."""
        self._tray.hide()
        os._exit(0)

    def _auto_hide(self):
        """Auto-hide after conversation timeout."""
        if self._state == JarvisState.CONVERSATION:
            self.hide_to_tray()

    def paintEvent(self, event):
        """Custom paint for glassmorphism effect."""
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        
        # Glassmorphism background
        path = QPainterPath()
        path.addRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        
        # Semi-transparent background with blur effect simulation
        gradient = QRadialGradient(self.width()/2, self.height()/2, self.width()*0.7)
        gradient.setColorAt(0, QColor(0, 6, 10, 230))
        gradient.setColorAt(1, QColor(0, 13, 20, 240))
        
        painter.setBrush(QBrush(gradient))
        painter.setPen(QPen(QColor(13, 51, 71, 180), 1))
        painter.drawRoundedRect(0, 0, self.width(), self.height(), 16, 16)
        
        # Glow effect at top
        glow_gradient = QRadialGradient(self.width()/2, 0, self.width()*0.5)
        glow_gradient.setColorAt(0, QColor(0, 212, 255, 30))
        glow_gradient.setColorAt(1, QColor(0, 212, 255, 0))
        
        painter.setBrush(QBrush(glow_gradient))
        painter.setPen(Qt.PenStyle.NoPen)
        painter.drawEllipse(-50, -50, self.width()+100, 150)
        
        painter.end()

    # ── State Management ──────────────────────────────────────────────

    @property
    def state(self) -> JarvisState:
        return self._state

    def set_state(self, state: JarvisState):
        """Transition to a new state."""
        if state == self._state:
            return
        
        old_state = self._state
        self._state = state
        
        # Handle state transitions
        if state == JarvisState.HIDDEN:
            self._animate_close()
        elif state == JarvisState.LISTENING:
            if old_state == JarvisState.HIDDEN:
                self._animate_open()
            self._start_listening()
        elif state == JarvisState.THINKING:
            self._start_thinking()
        elif state == JarvisState.CONVERSATION:
            self._start_conversation()
        
        self.state_changed.emit(state.value)

    def activate(self, state: JarvisState = JarvisState.LISTENING):
        """Activate JARVIS from any state."""
        if self._state == JarvisState.HIDDEN:
            self.set_state(state)
        else:
            # Already visible, just focus
            self.raise_()
            self.activateWindow()

    def hide_to_tray(self):
        """Minimize to tray with animation."""
        self.set_state(JarvisState.HIDDEN)

    def show_on_current_monitor(self):
        """Show on the monitor containing the cursor."""
        self._center_on_screen()
        self.show()
        self.raise_()
        self.activateWindow()

    # ── Animations ────────────────────────────────────────────────────

    def _animate_open(self):
        """Smooth open animation (250ms)."""
        # Calculate start position (slide up from below)
        start_pos = QPoint(self.x(), self.y() + 50)
        end_pos = QPoint(self.x(), self.y())
        
        self.move(start_pos)
        self.show()
        self.raise_()
        self.activateWindow()
        
        # Setup animations
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        
        self._pos_anim.setStartValue(start_pos)
        self._pos_anim.setEndValue(end_pos)
        
        self._open_group.start()

    def _animate_close(self):
        """Smooth close animation (200ms)."""
        end_pos = QPoint(self.x(), self.y() + 30)
        
        self._fade_close.setStartValue(1.0)
        self._fade_close.setEndValue(0.0)
        
        self._pos_close.setStartValue(self.pos())
        self._pos_close.setEndValue(end_pos)
        
        def on_close_done():
            self.hide()
            self.move(self.x(), self.y() - 30)  # Reset position
        
        self._close_group.finished.connect(on_close_done)
        self._close_group.start()

    def _start_listening(self):
        """Start listening state."""
        self._auto_hide_timer.stop()

    def _start_thinking(self):
        """Start thinking state."""
        pass

    def _start_conversation(self):
        """Start conversation state."""
        # Reset auto-hide timer
        self._auto_hide_timer.start(self._conversation_timeout)


class JarvisApp(QObject):
    """
    Main JARVIS application controller.
    
    Manages the state machine, wake word integration, and coordinates
    between the UI and backend systems.
    """
    
    # Signals
    state_changed = pyqtSignal(str)
    wake_word_detected = pyqtSignal()
    voice_command = pyqtSignal(str)
    
    def __init__(self, parent=None):
        super().__init__(parent)
        
        # Window
        self.window = JarvisWindow()
        
        # State
        self._state = JarvisState.HIDDEN
        self._muted = False
        self._wake_enabled = True
        
        # Callbacks
        self._on_wake: Optional[Callable] = None
        self._on_state_change: Optional[Callable] = None
        
        # Connect signals
        self.window.state_changed.connect(self._handle_state_change)
        self.window.wake_word_detected.connect(self._on_wake_word)
        
        # Wake word detection timer
        self._wake_check_timer = QTimer(self)
        self._wake_check_timer.timeout.connect(self._check_wake_word)
        self._wake_check_timer.start(100)  # Check every 100ms

    def set_wake_callback(self, callback: Callable):
        """Set callback for wake word events."""
        self._on_wake = callback

    def set_state_callback(self, callback: Callable):
        """Set callback for state changes."""
        self._on_state_change = callback

    def _handle_state_change(self, state_str: str):
        """Handle state changes from window."""
        if state_str == "mute":
            self._muted = True
        elif state_str == "unmute":
            self._muted = False
        elif state_str == "open_settings":
            pass  # Handle in main app
        else:
            try:
                self._state = JarvisState(state_str)
            except ValueError:
                pass
        
        if self._on_state_change:
            self._on_state_change(self._state)

    def _on_wake_word(self):
        """Handle wake word detection."""
        if self._muted or not self._wake_enabled:
            return
        
        self.window.activate(JarvisState.LISTENING)
        
        if self._on_wake:
            self._on_wake()

    def _check_wake_word(self):
        """Periodically check for wake word."""
        # This will be connected to the actual wake word detector
        pass

    # ── Public API ────────────────────────────────────────────────────

    def activate(self, state: JarvisState = JarvisState.LISTENING):
        """Activate JARVIS."""
        self.window.activate(state)

    def hide(self):
        """Hide to tray."""
        self.window.hide_to_tray()

    def set_state(self, state: JarvisState):
        """Set the current state."""
        self.window.set_state(state)

    def set_muted(self, muted: bool):
        """Set mute state."""
        self._muted = muted
        self.window._mute_action.setChecked(muted)

    def show(self):
        """Show the window."""
        self.window.show_on_current_monitor()

    def is_visible(self) -> bool:
        """Check if window is visible."""
        return self.window.isVisible()

    def set_conversation_timeout(self, ms: int):
        """Set auto-hide timeout for conversation state."""
        self.window._conversation_timeout = ms


def create_jarvis_app() -> JarvisApp:
    """Factory function to create the JarvisApp instance."""
    return JarvisApp()
