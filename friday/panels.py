"""FRIDAY's side panels.

Each panel owns its refresh cadence and renders only what the backend actually
reported.  Where a figure is unknown the panel says so; none of the values in
the layout reference are hard-coded here.
"""
from __future__ import annotations

import logging
from datetime import datetime

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QComboBox, QHBoxLayout, QLabel, QVBoxLayout,
                             QWidget)

from . import data
from . import theme as T
from .widgets import (EmptyState, IconButton, ListRow, MetricRow, Panel,
                      StatRow, Waveform, ui_font)

log = logging.getLogger("friday.panels")


class SystemOverviewPanel(Panel):
    """CPU / GPU / RAM / Storage, read from psutil and nvidia-smi."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("System Overview", parent)
        self.cpu = MetricRow("CPU", accent=T.CYAN)
        self.gpu = MetricRow("GPU", accent=T.VIOLET)
        self.ram = MetricRow("RAM", accent=T.BLUE)
        self.storage = MetricRow("Storage", accent=T.CYAN)
        for row in (self.cpu, self.gpu, self.ram, self.storage):
            self.content().addWidget(row)

        self._timer = QTimer(self)
        self._timer.setInterval(1500)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        r = data.system.snapshot()
        self.cpu.set_value(r.cpu)
        self.cpu.set_sub(r.cpu_name or "Processor")
        # An absent GPU reads as "No GPU telemetry", never as 0%.
        self.gpu.set_value(r.gpu)
        self.gpu.set_sub(r.gpu_name or "No GPU telemetry")
        self.ram.set_value(r.ram)
        self.ram.set_sub(r.ram_detail or "Memory")
        self.storage.set_value(r.storage)
        self.storage.set_sub(r.storage_detail or "System drive")


class NetworkPanel(Panel):
    """Throughput and reachability, measured rather than assumed."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Network", parent)
        self.down = StatRow("Download", mono_value=True)
        self.up = StatRow("Upload", mono_value=True)
        self.host = StatRow("Host", mono_value=True)
        self.battery = StatRow("Power")
        for row in (self.down, self.up, self.battery, self.host):
            self.content().addWidget(row)

        self._timer = QTimer(self)
        self._timer.setInterval(2000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        r = data.system.snapshot()
        if r.net_online is None:
            self.set_status("CHECKING", T.TEXT_FAINT)
        elif r.net_online:
            self.set_status("● ONLINE", T.GREEN)
        else:
            self.set_status("● OFFLINE", T.AMBER)

        self.down.set_value(None if r.net_down_mbps is None
                            else f"{r.net_down_mbps:.2f} Mbps")
        self.up.set_value(None if r.net_up_mbps is None
                          else f"{r.net_up_mbps:.2f} Mbps")
        self.host.set_value(r.host or None)

        if r.battery is None:
            self.battery.set_value(None)
        else:
            plugged = " · charging" if r.battery_plugged else " · on battery"
            colour = (T.GREEN if r.battery > 40 else
                      T.AMBER if r.battery > 15 else T.CRIMSON)
            self.battery.set_value(f"{r.battery:.0f}%{plugged}", colour)


class VoiceStatusPanel(Panel):
    """FRIDAY's microphone truth.

    The waveform is fed exclusively by :meth:`push_level` from the real audio
    callback.  Nothing here animates on a timer, so a quiet room draws a quiet
    trace and "HEARING YOU" only appears when voice activity was detected.
    """

    mute_toggled = pyqtSignal(bool)
    device_requested = pyqtSignal(str)
    test_requested = pyqtSignal()
    wake_toggled = pyqtSignal(bool)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Voice Status", parent)
        self._muted = False
        self._peak = 0.0
        self._level = 0.0
        self._state = "idle"

        self.wave = Waveform(self)
        self.wave.setMinimumHeight(30)
        self.wave.setMaximumHeight(34)
        self.content().addWidget(self.wave)

        self.state_label = QLabel("READY")
        self.state_label.setFont(ui_font(T.FS_LABEL, QFont.Weight.DemiBold))
        self.state_label.setStyleSheet(
            f"color:{T.CYAN}; letter-spacing:1.2px; background:transparent;")
        self.content().addWidget(self.state_label)

        self.device = QComboBox(self)
        self.device.setFont(ui_font(T.FS_MICRO))
        self.device.setCursor(Qt.CursorShape.PointingHandCursor)
        # Device names are long and arbitrary; elide them rather than let the
        # combo dictate the column's width.
        self.device.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.device.setMinimumContentsLength(10)
        self.device.setStyleSheet(f"""
            QComboBox {{ color:{T.TEXT_DIM}; background:{T.BG_INPUT};
                border:1px solid {T.BORDER_SUBTLE}; border-radius:{T.RADIUS_SM}px;
                padding:5px 8px; }}
            QComboBox:hover {{ border-color:{T.BORDER_STRONG}; }}
            QComboBox QAbstractItemView {{ color:{T.TEXT};
                background:{T.BG_PANEL_SOLID}; selection-background-color:{T.BLUE}; }}
        """)
        self.content().addWidget(self.device)

        self.level_row = StatRow("Input level", mono_value=True)
        self.peak_row = StatRow("Peak", mono_value=True)
        self.vad_row = StatRow("Voice activity", mono_value=True)
        self.wake_row = StatRow("Wake word")
        for row in (self.level_row, self.peak_row, self.vad_row, self.wake_row):
            self.content().addWidget(row)

        controls = QHBoxLayout()
        controls.setSpacing(6)
        self.mute_btn = IconButton("Mute", "Mute or unmute the microphone", self,
                                   checkable=True)
        self.mute_btn.clicked.connect(self._on_mute)
        self.test_btn = IconButton("Test", "Speak to confirm input is arriving", self)
        self.test_btn.clicked.connect(self.test_requested.emit)
        controls.addWidget(self.mute_btn)
        controls.addWidget(self.test_btn)
        self.content().addLayout(controls)

        self._populate_devices()

        # Only decays the meter; the level itself is never synthesised.
        self._decay = QTimer(self)
        self._decay.setInterval(90)
        self._decay.timeout.connect(self._decay_peak)
        self._decay.start()

    def _populate_devices(self) -> None:
        """List real input devices; disable the selector when none are found."""
        names: list[str] = []
        try:
            import sounddevice as sd
            for dev in sd.query_devices():
                if dev.get("max_input_channels", 0) > 0:
                    names.append(dev["name"])
        except Exception as exc:
            log.info("FRIDAY: could not enumerate microphones: %s", exc)

        if names:
            self.device.addItems(names)
            try:
                import sounddevice as sd
                default = sd.query_devices(kind="input")["name"]
                if default in names:
                    self.device.setCurrentText(default)
            except Exception:
                pass
            self.device.currentTextChanged.connect(self.device_requested.emit)
        else:
            self.device.addItem("No input device detected")
            self.device.setEnabled(False)

    # ---- live input ----------------------------------------------------

    def push_level(self, level: float) -> None:
        """Feed one real microphone amplitude sample (0..1)."""
        level = 0.0 if self._muted else max(0.0, min(1.0, float(level)))
        self._level = level
        self._peak = max(self._peak, level)
        self.wave.push(level)
        self.level_row.set_value(f"{level * 100:4.0f}%")
        self.peak_row.set_value(f"{self._peak * 100:4.0f}%")

    def set_vad(self, probability: float | None) -> None:
        self.vad_row.set_value(None if probability is None
                               else f"{probability * 100:4.0f}%")

    def set_wake_word(self, armed: bool | None) -> None:
        if armed is None:
            self.wake_row.set_value(None)
        else:
            self.wake_row.set_value("Armed" if armed else "Paused",
                                    T.GREEN if armed else T.TEXT_FAINT)

    def set_state(self, state: str, label: str, accent: str) -> None:
        self._state = state
        if self._muted:
            self.state_label.setText("MICROPHONE MUTED")
            self.state_label.setStyleSheet(
                f"color:{T.AMBER}; letter-spacing:1.2px; background:transparent;")
            return
        self.state_label.setText(label)
        self.state_label.setStyleSheet(
            f"color:{accent}; letter-spacing:1.2px; background:transparent;")
        self.wave.set_accent(accent)

    def set_muted(self, muted: bool) -> None:
        self._muted = bool(muted)
        self.mute_btn.setChecked(self._muted)
        self.mute_btn.setText("Unmute" if self._muted else "Mute")
        self.wave.set_active(not self._muted)
        self.set_status("MUTED" if self._muted else "", T.AMBER)
        if self._muted:
            self.state_label.setText("MICROPHONE MUTED")
            self.state_label.setStyleSheet(
                f"color:{T.AMBER}; letter-spacing:1.2px; background:transparent;")

    def _on_mute(self) -> None:
        self.set_muted(self.mute_btn.isChecked())
        self.mute_toggled.emit(self._muted)

    def _decay_peak(self) -> None:
        self._peak *= 0.965
        if self._peak < 0.01:
            self._peak = 0.0


class RecentTasksPanel(Panel):
    """Genuine routing events from the metrics log — not a scripted list."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Recent Tasks", parent)
        self._rows: list[QWidget] = []
        self._empty = EmptyState("No tasks recorded yet.")
        self.content().addWidget(self._empty)

        self._timer = QTimer(self)
        self._timer.setInterval(6000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        tasks = data.recent_tasks(5)
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        if not tasks:
            self._empty.show()
            self.set_status("")
            return
        self._empty.hide()

        for entry in tasks:
            ok = bool(entry.get("ok"))
            model = str(entry.get("model", "unknown"))
            task = str(entry.get("task", ""))
            latency = entry.get("latency")
            meta = f"{latency:.1f}s" if isinstance(latency, (int, float)) else ""
            title = f"{task or 'request'} · {model.split('/')[-1]}"
            row = ListRow(title, meta, T.GREEN if ok else T.CRIMSON, self.body)
            row.setToolTip(entry.get("error") or ("Completed" if ok else "Failed"))
            self.content().addWidget(row)
            self._rows.append(row)
        self.set_status(f"{len(tasks)}")


class AIControlPanel(Panel):
    """Live routing state from core.ai, plus real manual override control."""

    override_changed = pyqtSignal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__("AI Control Center", parent)

        self.provider = StatRow("Provider")
        self.model = StatRow("Model", mono_value=True)
        self.routing = StatRow("Routing")
        self.task = StatRow("Task class")
        self.latency = StatRow("Latency", mono_value=True)
        self.state = StatRow("Last call")
        for row in (self.provider, self.model, self.routing, self.task,
                    self.latency, self.state):
            self.content().addWidget(row)

        self.reason = EmptyState("Waiting for the first routed request.")
        self.reason.setWordWrap(True)
        self.content().addWidget(self.reason)

        picker = QHBoxLayout()
        picker.setSpacing(6)
        self.model_box = QComboBox(self)
        self.model_box.setFont(ui_font(T.FS_MICRO))
        self.model_box.setCursor(Qt.CursorShape.PointingHandCursor)
        self.model_box.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
        self.model_box.setMinimumContentsLength(10)
        self.model_box.setStyleSheet(f"""
            QComboBox {{ color:{T.TEXT_DIM}; background:{T.BG_INPUT};
                border:1px solid {T.BORDER_SUBTLE}; border-radius:{T.RADIUS_SM}px;
                padding:5px 8px; }}
            QComboBox:hover {{ border-color:{T.BORDER_STRONG}; }}
            QComboBox QAbstractItemView {{ color:{T.TEXT};
                background:{T.BG_PANEL_SOLID}; selection-background-color:{T.BLUE}; }}
        """)
        self._overrides = self._override_names()
        self.model_box.addItem("Automatic (OmniRoute)")
        self.model_box.addItems(self._overrides)
        self.model_box.activated.connect(self._on_pick)
        picker.addWidget(self.model_box, 1)
        self.content().addLayout(picker)

        self._timer = QTimer(self)
        self._timer.setInterval(1800)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def _override_names(self) -> list[str]:
        try:
            from core import ai
            return sorted(ai.OVERRIDES)
        except Exception:
            return []

    def _on_pick(self, index: int) -> None:
        name = None if index == 0 else self.model_box.itemText(index)
        try:
            data.set_ai_override(name)
        except Exception as exc:
            log.error("FRIDAY: could not change routing: %s", exc)
            self.set_status("CHANGE FAILED", T.CRIMSON)
            return
        self.override_changed.emit(name)
        self.refresh()

    def refresh(self) -> None:
        r = data.ai_reading()
        self.provider.set_value(r.provider)
        self.model.set_value(r.served or r.model)
        self.routing.set_value(r.routing,
                               T.AMBER if r.override else T.GREEN)
        self.task.set_value(r.task)
        self.latency.set_value(None if r.latency_ms is None else f"{r.latency_ms} ms")

        if r.state:
            ok = r.state == "ok"
            self.state.set_value("Succeeded" if ok else r.state,
                                 T.GREEN if ok else T.CRIMSON)
            self.set_status("● LIVE" if ok else "● FAULT",
                            T.GREEN if ok else T.CRIMSON)
        else:
            self.state.set_value(None)
            self.set_status("IDLE", T.TEXT_FAINT)

        self.reason.setText(r.reason or "Waiting for the first routed request.")

        # Keep the selector in step with the persisted override.
        want = 0 if not r.override else self.model_box.findText(r.override)
        if want >= 0 and want != self.model_box.currentIndex():
            self.model_box.setCurrentIndex(want)


class WorkspacePanel(Panel):
    """The folder FRIDAY is operating in — the real path, not a project name."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Current Workspace", parent)
        self.root = StatRow("Root", mono_value=True)
        self.files = StatRow("Files")
        self.attached = StatRow("Attached")
        for row in (self.root, self.files, self.attached):
            self.content().addWidget(row)
        self.refresh()

    def refresh(self, attached: str | None = None) -> None:
        root = data.REPO_ROOT
        self.root.set_value(root.name)
        self.root.setToolTip(str(root))
        try:
            count = sum(1 for _ in root.glob("*"))
            self.files.set_value(f"{count} entries")
        except Exception:
            self.files.set_value(None)
        self.attached.set_value(attached)


class ClipboardPanel(Panel):
    """A preview of clipboard text, refreshed only on demand.

    Polling the clipboard continuously would mean this panel quietly displays
    whatever is copied — including passwords — so the read is manual.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__("Clipboard Preview", parent)
        self._text = EmptyState("Press Read to preview the clipboard.")
        self._text.setWordWrap(True)
        self.content().addWidget(self._text)

        row = QHBoxLayout()
        row.setSpacing(6)
        self.read_btn = IconButton("Read", "Show the current clipboard text", self)
        self.read_btn.clicked.connect(self.refresh)
        self.clear_btn = IconButton("Hide", "Clear this preview", self)
        self.clear_btn.clicked.connect(self._hide_text)
        row.addWidget(self.read_btn)
        row.addWidget(self.clear_btn)
        self.content().addLayout(row)

    def refresh(self) -> None:
        text = data.clipboard_preview()
        if text is None:
            self._text.setText("Clipboard is empty or unavailable.")
            self.set_status("")
        else:
            self._text.setText(text)
            self.set_status("1 item", T.TEXT_FAINT)

    def _hide_text(self) -> None:
        self._text.setText("Press Read to preview the clipboard.")
        self.set_status("")


class RemindersPanel(Panel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("Reminders", parent)
        self._rows: list[QWidget] = []
        self._empty = EmptyState("Loading…")
        self.content().addWidget(self._empty)

        self._timer = QTimer(self)
        self._timer.setInterval(20000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        items = data.reminders()
        for row in self._rows:
            row.setParent(None)
            row.deleteLater()
        self._rows.clear()

        if items is None:
            self._empty.setText("Reminder module not configured.")
            self._empty.show()
            self.set_status("")
            return
        if not items:
            self._empty.setText("No upcoming reminders.")
            self._empty.show()
            self.set_status("0", T.TEXT_FAINT)
            return

        self._empty.hide()
        for item in items[:4]:
            if isinstance(item, dict):
                title = str(item.get("text") or item.get("title") or "Reminder")
                when = str(item.get("when") or item.get("time") or "")
            else:
                title, when = str(item), ""
            row = ListRow(title, when, T.VIOLET, self.body)
            self.content().addWidget(row)
            self._rows.append(row)
        self.set_status(f"{len(items)} upcoming", T.TEXT_FAINT)


class MemoryPanel(Panel):
    def __init__(self, parent: QWidget | None = None):
        super().__init__("Memory Status", parent)
        self.entries = StatRow("Entries", mono_value=True)
        self.size = StatRow("Store size", mono_value=True)
        self.content().addWidget(self.entries)
        self.content().addWidget(self.size)

        self._timer = QTimer(self)
        self._timer.setInterval(15000)
        self._timer.timeout.connect(self.refresh)
        self._timer.start()
        self.refresh()

    def refresh(self) -> None:
        count, size = data.memory_reading()
        if count is None and size is None:
            self.entries.set_value(None)
            self.size.set_value(None)
            self.set_status("NO STORE", T.TEXT_FAINT)
            return
        self.entries.set_value(None if count is None else f"{count}")
        self.size.set_value(size)
        self.set_status("● ACTIVE", T.GREEN)
