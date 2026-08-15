from __future__ import annotations

import json
import math
import os
import platform
import random
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import psutil

from friday import FridayModeManager, FridayStateController

from PyQt6.QtCore import (
    QEasingCurve, QMimeData, QObject, QPointF, QRect, QRectF, QSize, Qt,
    QTimer, QUrl, pyqtSignal,
)
from PyQt6.QtGui import (
    QBrush, QColor, QDragEnterEvent, QDropEvent, QFont, QFontDatabase,
    QIcon, QKeySequence, QLinearGradient, QPainter, QPainterPath, QPen,
    QPixmap, QRadialGradient, QShortcut, QTextCharFormat,
)
from PyQt6.QtWidgets import (
    QApplication, QCalendarWidget, QComboBox, QFileDialog, QFrame, QGridLayout, QInputDialog,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMenu, QPushButton,
    QScrollArea, QSizePolicy, QStackedWidget, QSystemTrayIcon, QTextEdit,
    QVBoxLayout, QWidget, QProgressBar,
)

def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent

BASE_DIR   = _base_dir()
CONFIG_DIR = BASE_DIR / "config"
API_FILE   = CONFIG_DIR / "api_keys.json"

_DEFAULT_W, _DEFAULT_H = 980, 700
_MIN_W,     _MIN_H     = 820, 580
_LEFT_W  = 148
_RIGHT_W = 340

_OS = platform.system()  # "Windows" | "Darwin" | "Linux"


class C:
    BG        = "#00060a"
    PANEL     = "#010d14"
    PANEL2    = "#010f18"
    BORDER    = "#0d3347"
    BORDER_B  = "#1a5c7a"
    BORDER_A  = "#0f4060"
    PRI       = "#00d4ff"
    PRI_DIM   = "#007a99"
    PRI_GHO   = "#001f2e"
    ACC       = "#ff6b00"
    ACC2      = "#ffcc00"
    GREEN     = "#00ff88"
    GREEN_D   = "#00aa55"
    RED       = "#ff3355"
    MUTED_C   = "#ff3366"
    TEXT      = "#8ffcff"
    TEXT_DIM  = "#3a8a9a"
    TEXT_MED  = "#5ab8cc"
    WHITE     = "#d8f8ff"
    DARK      = "#000d14"
    BAR_BG    = "#011520"


def qcol(h: str, a: int = 255) -> QColor:
    c = QColor(h); c.setAlpha(a); return c


# ── themes ── palette swap drives paintEvent colors instantly; styled
# widgets are rebuilt by MainWindow._apply_theme.
_DEFAULT_PALETTE = {k: v for k, v in vars(C).items() if not k.startswith("__")}
_BATTLE_PALETTE = dict(_DEFAULT_PALETTE, **{
    "BG": "#0a0002", "PANEL": "#160104", "PANEL2": "#1c0206",
    "BORDER": "#47100f", "BORDER_B": "#7a1a22", "BORDER_A": "#601014",
    "PRI": "#ff2135", "PRI_DIM": "#991020", "PRI_GHO": "#2e0410",
    "ACC": "#ff6b00", "ACC2": "#ffb347",
    "GREEN": "#ffaa33", "GREEN_D": "#aa6a11",
    "TEXT": "#ffb3ba", "TEXT_DIM": "#9a3a44", "TEXT_MED": "#cc5a66",
    "WHITE": "#ffdfe2", "DARK": "#120004", "BAR_BG": "#230208",
})
THEMES = {"normal": _DEFAULT_PALETTE, "battle": _BATTLE_PALETTE}

def _apply_palette(name: str):
    for k, v in THEMES.get(name, _DEFAULT_PALETTE).items():
        setattr(C, k, v)

class _SysMetrics:
    def __init__(self):
        self.cpu  = 0.0
        self.mem  = 0.0
        self.net  = 0.0   
        self.gpu  = -1.0  
        self.tmp  = -1.0  
        self._lock = threading.Lock()
        self._last_net = psutil.net_io_counters()
        self._last_net_t = time.time()
        self._running = True
        t = threading.Thread(target=self._loop, daemon=True)
        t.start()

    def _loop(self):
        while self._running:
            try:
                self._update()
            except Exception:
                pass
            time.sleep(3.0)  # Reduced from 1.5s to lower CPU usage

    def _update(self):
        cpu = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory().percent

        nc  = psutil.net_io_counters()
        now = time.time()
        dt  = now - self._last_net_t
        if dt > 0:
            sent = (nc.bytes_sent - self._last_net.bytes_sent) / dt
            recv = (nc.bytes_recv - self._last_net.bytes_recv) / dt
            net  = (sent + recv) / (1024 * 1024)
        else:
            net = 0.0
        self._last_net   = nc
        self._last_net_t = now

        gpu = self._get_gpu()

        tmp = self._get_temp()

        with self._lock:
            self.cpu = cpu
            self.mem = mem
            self.net = net
            self.gpu = gpu
            self.tmp = tmp

    def _get_gpu(self) -> float:
        # ponytail: probe once — a machine without nvidia-smi won't grow one;
        # retrying every 2s spawns a process per tick for nothing
        if getattr(self, "_gpu_dead", False):
            return -1.0
        # NVIDIA
        try:
            r = subprocess.run(
                ["nvidia-smi", "--query-gpu=utilization.gpu",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2
            )
            if r.returncode == 0:
                vals = [float(v.strip()) for v in r.stdout.strip().split("\n") if v.strip()]
                if vals:
                    return sum(vals) / len(vals)
        except Exception:
            pass

        # AMD (Linux)
        if _OS == "Linux":
            try:
                r = subprocess.run(
                    ["rocm-smi", "--showuse", "--csv"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    for line in r.stdout.strip().split("\n"):
                        parts = line.split(",")
                        if len(parts) >= 2:
                            try:
                                return float(parts[1].strip().replace("%", ""))
                            except ValueError:
                                pass
            except Exception:
                pass

            # Intel GPU (Linux)
            try:
                r = subprocess.run(
                    ["intel_gpu_top", "-J", "-s", "500"],
                    capture_output=True, text=True, timeout=1
                )
                if r.returncode == 0 and "Render/3D" in r.stdout:
                    import re
                    m = re.search(r'"busy":\s*([\d.]+)', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        # macOS — powermetrics (GPU Engine)
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["sudo", "-n", "powermetrics", "-n", "1", "-i", "500",
                     "--samplers", "gpu_power"],
                    capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0 and "GPU" in r.stdout:
                    import re
                    m = re.search(r'GPU\s+Active:\s+([\d.]+)%', r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        self._gpu_dead = True
        return -1.0

    def _get_temp(self) -> float:
        if getattr(self, "_temp_dead", False):
            return -1.0
        try:
            temps = psutil.sensors_temperatures()
            candidates = ["coretemp", "k10temp", "cpu_thermal", "acpitz",
                          "cpu-thermal", "zenpower", "it8688"]
            for name in candidates:
                if name in temps:
                    entries = temps[name]
                    if entries:
                        return entries[0].current
            for entries in temps.values():
                if entries:
                    return entries[0].current
        except Exception:
            pass
        if _OS == "Darwin":
            try:
                r = subprocess.run(
                    ["osx-cpu-temp"], capture_output=True, text=True, timeout=2
                )
                if r.returncode == 0:
                    import re
                    m = re.search(r"([\d.]+)", r.stdout)
                    if m:
                        return float(m.group(1))
            except Exception:
                pass

        if _OS == "Windows":
            try:
                r = subprocess.run(
                    ["powershell", "-Command",
                     "(Get-WmiObject MSAcpi_ThermalZoneTemperature -Namespace root/wmi).CurrentTemperature"],
                    capture_output=True, text=True, timeout=3
                )
                if r.returncode == 0 and r.stdout.strip():
                    raw = float(r.stdout.strip().split("\n")[0])
                    return (raw / 10.0) - 273.15
            except Exception:
                pass

        self._temp_dead = True   # ponytail: same as GPU — don't re-probe failures
        return -1.0

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "cpu": self.cpu,
                "mem": self.mem,
                "net": self.net,
                "gpu": self.gpu,
                "tmp": self.tmp,
            }


_metrics = _SysMetrics()

class HudCanvas(QWidget):
    def __init__(self, face_path: str, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        self.muted    = False
        self.speaking = False
        self.state    = "INITIALISING"
        self.combat   = False

        self._tick       = 0
        self._scale      = 1.0
        self._tgt_scale  = 1.0
        self._halo       = 55.0
        self._tgt_halo   = 55.0
        self._last_t     = time.time()
        self._scan       = 0.0
        self._scan2      = 180.0
        self._rings      = [0.0, 120.0, 240.0]
        self._pulses: list[float] = [0.0, 50.0, 100.0]
        self._blink      = True
        self._blink_tick = 0
        self._particles: list[list[float]] = []
        self._face_px: QPixmap | None = None
        self._sentinel_px: QPixmap | None = None
        self._load_face(face_path)
        self._load_sentinel()

        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def _load_face(self, path: str):
        try:
            from PIL import Image, ImageDraw
            import io
            img = Image.open(path).convert("RGBA")
            sz  = min(img.size)
            img = img.resize((sz, sz), Image.LANCZOS)
            mk  = Image.new("L", (sz, sz), 0)
            ImageDraw.Draw(mk).ellipse((2, 2, sz - 2, sz - 2), fill=255)
            img.putalpha(mk)
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            px = QPixmap(); px.loadFromData(buf.getvalue())
            self._face_px = px
        except Exception:
            self._face_px = None

    def _load_sentinel(self):
        """Load the original Battle-mode centerpiece if the optional asset exists."""
        path = BASE_DIR / "assets" / "jarvis-sentinel.png"
        if path.exists():
            px = QPixmap(str(path))
            self._sentinel_px = px if not px.isNull() else None

    def _step(self):
        self._tick += 1
        now = time.time()
        if now - self._last_t > (0.12 if self.speaking else 0.5):
            if self.speaking:
                self._tgt_scale = random.uniform(1.06, 1.14)
                self._tgt_halo  = random.uniform(145, 190)
            elif self.muted:
                self._tgt_scale = random.uniform(0.998, 1.002)
                self._tgt_halo  = random.uniform(15, 28)
            else:
                self._tgt_scale = random.uniform(1.001, 1.008)
                self._tgt_halo  = random.uniform(48, 68)
            self._last_t = now

        sp = 0.38 if self.speaking else 0.15
        self._scale += (self._tgt_scale - self._scale) * sp
        self._halo  += (self._tgt_halo  - self._halo)  * sp

        speeds = [1.3, -0.9, 2.0] if self.speaking else [0.55, -0.35, 0.9]
        for i, spd in enumerate(speeds):
            self._rings[i] = (self._rings[i] + spd) % 360

        self._scan  = (self._scan  + (3.0 if self.speaking else 1.3)) % 360
        self._scan2 = (self._scan2 + (-2.0 if self.speaking else -0.75)) % 360

        fw  = min(self.width(), self.height())
        lim = fw * 0.74
        spd = 4.2 if self.speaking else 2.0
        self._pulses = [r + spd for r in self._pulses if r + spd < lim]
        if len(self._pulses) < 3 and random.random() < (0.07 if self.speaking else 0.025):
            self._pulses.append(0.0)

        if self.speaking and random.random() < 0.28:
            cx, cy = self.width() / 2, self.height() / 2
            ang = random.uniform(0, 2 * math.pi)
            r_s = fw * 0.28
            self._particles.append([
                cx + math.cos(ang) * r_s, cy + math.sin(ang) * r_s,
                math.cos(ang) * random.uniform(0.9, 2.4),
                math.sin(ang) * random.uniform(0.9, 2.4) - 0.4, 1.0,
            ])
        self._particles = [
            [p[0]+p[2], p[1]+p[3], p[2]*0.97, p[3]*0.97, p[4]-0.028]
            for p in self._particles if p[4] > 0
        ]

        self._blink_tick += 1
        if self._blink_tick >= 38:
            self._blink = not self._blink
            self._blink_tick = 0
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG))

        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        fw = min(W, H)

        # grid dots
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)

        if self.combat:
            p.setPen(QPen(qcol(C.PRI, 110), 1))
            p.drawLine(QPointF(12, 16), QPointF(W * 0.28, 16))
            p.drawLine(QPointF(W * 0.72, 16), QPointF(W - 12, 16))
            p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
            p.setPen(QPen(qcol(C.PRI, 210), 1))
            p.drawText(QRectF(W * 0.28, 7, W * 0.44, 18),
                       Qt.AlignmentFlag.AlignCenter, "SENTINEL CORE // ARMED")

        r_face = fw * 0.31

        # halo glow
        for i in range(10):
            r   = r_face * (1.8 - i * 0.08)
            frc = 1.0 - i / 10
            a   = max(0, min(255, int(self._halo * 0.085 * frc)))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - r, cy - r, r * 2, r * 2))

        # pulse rings
        for pr in self._pulses:
            a   = max(0, int(230 * (1.0 - pr / (fw * 0.74))))
            col = qcol(C.MUTED_C if self.muted else C.PRI, a)
            p.setPen(QPen(col, 1.5)); p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(QRectF(cx - pr, cy - pr, pr * 2, pr * 2))

        # spinning arc rings
        for idx, (r_frac, w_r, arc_l, gap) in enumerate(
            [(0.48, 3, 115, 78), (0.40, 2, 78, 55), (0.32, 1, 56, 40)]
        ):
            ring_r = fw * r_frac
            base   = self._rings[idx]
            a_val  = max(0, min(255, int(self._halo * (1.0 - idx * 0.18))))
            col    = qcol(C.MUTED_C if self.muted else C.PRI, a_val)
            p.setPen(QPen(col, w_r)); p.setBrush(Qt.BrushStyle.NoBrush)
            angle = base
            rect  = QRectF(cx - ring_r, cy - ring_r, ring_r * 2, ring_r * 2)
            while angle < base + 360:
                p.drawArc(rect, int(angle * 16), int(arc_l * 16))
                angle += arc_l + gap

        # scanners
        sr = fw * 0.50
        sa = min(255, int(self._halo * 1.5))
        ex = 75 if self.speaking else 44
        p.setPen(QPen(qcol(C.MUTED_C if self.muted else C.PRI, sa), 2.5))
        p.setBrush(Qt.BrushStyle.NoBrush)
        srect = QRectF(cx - sr, cy - sr, sr * 2, sr * 2)
        p.drawArc(srect, int(self._scan * 16), int(ex * 16))
        p.setPen(QPen(qcol(C.ACC, sa // 2), 1.5))
        p.drawArc(srect, int(self._scan2 * 16), int(ex * 16))

        # tick marks
        t_out, t_in = fw * 0.497, fw * 0.474
        p.setPen(QPen(qcol(C.PRI, 140), 1))
        for deg in range(0, 360, 10):
            rad = math.radians(deg)
            inn = t_in if deg % 30 == 0 else t_in + 6
            p.drawLine(
                QPointF(cx + t_out * math.cos(rad), cy - t_out * math.sin(rad)),
                QPointF(cx + inn  * math.cos(rad), cy - inn  * math.sin(rad)),
            )

        # crosshair
        ch_r, gap_h = fw * 0.51, fw * 0.16
        p.setPen(QPen(qcol(C.PRI, int(self._halo * 0.5)), 1))
        p.drawLine(QPointF(cx - ch_r, cy), QPointF(cx - gap_h, cy))
        p.drawLine(QPointF(cx + gap_h, cy), QPointF(cx + ch_r, cy))
        p.drawLine(QPointF(cx, cy - ch_r), QPointF(cx, cy - gap_h))
        p.drawLine(QPointF(cx, cy + gap_h), QPointF(cx, cy + ch_r))

        # corner brackets
        bl = 24
        bc = qcol(C.PRI, 210)
        hl, hr = cx - fw // 2, cx + fw // 2
        ht, hb = cy - fw // 2, cy + fw // 2
        p.setPen(QPen(bc, 2))
        for bx, by, dx, dy in [(hl,ht,1,1),(hr,ht,-1,1),(hl,hb,1,-1),(hr,hb,-1,-1)]:
            p.drawLine(QPointF(bx, by), QPointF(bx + dx * bl, by))
            p.drawLine(QPointF(bx, by), QPointF(bx, by + dy * bl))

        # Battle mode owns the center visual, even if the user normally has
        # the FRIDAY persona selected. This makes the transition structural,
        # not just a palette swap.
        if self.combat and self._sentinel_px:
            fsz = int(fw * 1.00 * self._scale)
            scaled = self._sentinel_px.scaled(
                fsz, fsz, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation)
            p.drawPixmap(int(cx - scaled.width() / 2), int(cy - scaled.height() * 0.46), scaled)
        elif self._face_px:
            fsz    = int(fw * 0.62 * self._scale)
            scaled = self._face_px.scaled(
                fsz, fsz,
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            p.drawPixmap(int(cx - fsz / 2), int(cy - fsz / 2), scaled)
        else:
            orb_r = int(fw * 0.27 * self._scale)
            oc    = (200, 0, 50) if self.muted else (0, 60, 110)
            for i in range(8, 0, -1):
                r2  = int(orb_r * i / 8)
                frc = i / 8
                a   = max(0, min(255, int(self._halo * 1.1 * frc)))
                p.setBrush(QBrush(QColor(int(oc[0]*frc), int(oc[1]*frc), int(oc[2]*frc), a)))
                p.setPen(Qt.PenStyle.NoPen)
                p.drawEllipse(QRectF(cx - r2, cy - r2, r2 * 2, r2 * 2))
            p.setPen(QPen(qcol(C.PRI, min(255, int(self._halo * 2))), 1))
            p.setFont(QFont("Courier New", 13, QFont.Weight.Bold))
            p.drawText(QRectF(cx - 100, cy - 14, 200, 28),
                       Qt.AlignmentFlag.AlignCenter,
                       "J.A.R.V.I.S // CORE" if self.combat else "J.A.R.V.I.S")

        # particles
        for pt in self._particles:
            a = max(0, min(255, int(pt[4] * 255)))
            p.setPen(Qt.PenStyle.NoPen)
            p.setBrush(QBrush(qcol(C.PRI, a)))
            p.drawEllipse(QPointF(pt[0], pt[1]), 2.5, 2.5)

        # status text
        sy = cy + fw * 0.40
        if self.muted:
            txt, col = "⊘  MUTED",     qcol(C.MUTED_C)
        elif self.speaking:
            txt, col = "●  SPEAKING",  qcol(C.ACC)
        elif self.state == "THINKING":
            sym = "◈" if self._blink else "◇"
            txt, col = f"{sym}  THINKING",   qcol(C.ACC2)
        elif self.state == "PROCESSING":
            sym = "▷" if self._blink else "▶"
            txt, col = f"{sym}  PROCESSING", qcol(C.ACC2)
        elif self.state == "LISTENING":
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  LISTENING",  qcol(C.GREEN)
        else:
            sym = "●" if self._blink else "○"
            txt, col = f"{sym}  {self.state}", qcol(C.PRI)

        p.setPen(QPen(col, 1))
        p.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        p.drawText(QRectF(0, sy, W, 26), Qt.AlignmentFlag.AlignCenter, txt)

        # waveform
        wy = sy + 30
        N, bw = 36, 8
        wx0 = (W - N * bw) / 2
        for i in range(N):
            if self.muted:
                hgt, cl = 2, qcol(C.MUTED_C)
            elif self.speaking:
                hgt = random.randint(3, 20)
                cl  = qcol(C.PRI) if hgt > 12 else qcol(C.PRI_DIM)
            else:
                hgt = int(3 + 2 * math.sin(self._tick * 0.09 + i * 0.6))
                cl  = qcol(C.BORDER_B)
            p.fillRect(QRectF(wx0 + i * bw, wy + 20 - hgt, bw - 1, hgt), cl)

class FaceCanvas(QWidget):
    """FRIDAY's living face — eyes, brows, mouth; emotions morph smoothly;
    the mouth lip-syncs to real output audio via `level` (0..1)."""

    # raiL/raiR: brow raise • tilt: +1 kind/sad, -1 angry • eye: openness
    # wink: right eye shut • px/py: pupil gaze • smile: -1..1 • smirk: asym
    # open: resting mouth-open • tint: glow override
    EMOTIONS = {
        "neutral":   dict(raiL=.30, raiR=.30, tilt=0.0,  eye=1.0,  wink=0, px=0.0,  py=0.0,  smile=.28, smirk=0.0, open=0.0, tint=None),
        "happy":     dict(raiL=.60, raiR=.60, tilt=.15,  eye=.90,  wink=0, px=0.0,  py=0.0,  smile=.95, smirk=0.0, open=.10, tint=None),
        "caring":    dict(raiL=.40, raiR=.40, tilt=.45,  eye=.85,  wink=0, px=0.0,  py=.08,  smile=.55, smirk=0.0, open=0.0, tint=None),
        "teasing":   dict(raiL=.65, raiR=.25, tilt=0.0,  eye=.95,  wink=1, px=.15,  py=0.0,  smile=.70, smirk=.55, open=0.0, tint=None),
        "sarcastic": dict(raiL=.90, raiR=.15, tilt=-.10, eye=.80,  wink=0, px=.25,  py=0.0,  smile=.25, smirk=.65, open=0.0, tint=None),
        "angry":     dict(raiL=.10, raiR=.10, tilt=-1.0, eye=.50,  wink=0, px=0.0,  py=.05,  smile=-.60, smirk=0.0, open=.08, tint="#ff3344"),
        "sad":       dict(raiL=.35, raiR=.35, tilt=.85,  eye=.70,  wink=0, px=0.0,  py=.18,  smile=-.55, smirk=0.0, open=0.0, tint=None),
        "surprised": dict(raiL=1.0, raiR=1.0, tilt=.10,  eye=1.25, wink=0, px=0.0,  py=-.05, smile=.15, smirk=0.0, open=.55, tint=None),
        "thinking":  dict(raiL=.85, raiR=.30, tilt=0.0,  eye=.85,  wink=0, px=.35,  py=-.30, smile=.15, smirk=.25, open=0.0, tint=None),
        "sleepy":    dict(raiL=.15, raiR=.15, tilt=.20,  eye=.06,  wink=0, px=0.0,  py=.20,  smile=.20, smirk=0.0, open=0.0, tint=None),
        "serious":   dict(raiL=.10, raiR=.10, tilt=-.72, eye=.66,  wink=0, px=0.0,  py=.04, smile=-.10, smirk=0.0, open=0.0, tint="#ff3355"),
    }
    _NUM = [k for k in EMOTIONS["neutral"] if k != "tint"]

    def __init__(self, face_path: str | None = None, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setMinimumSize(300, 300)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.state    = "INITIALISING"
        self.speaking = False
        self.muted    = False
        self.level    = 0.0                    # live output-audio RMS 0..1
        self.emotion: str | None = None        # explicit emotion (from tool)
        self._emotion_until = 0.0
        self._cur  = dict(self.EMOTIONS["neutral"]); self._cur.pop("tint")
        self._tint: str | None = None
        self._talk  = 0.0
        self._blink = 1.0
        self._next_blink = time.time() + 3.0
        self._rings = [0.0, 200.0]
        self.combat = False
        self.transparent_backdrop = False
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._tmr.start(16)

    def set_emotion(self, name: str):
        if name in self.EMOTIONS:
            self.emotion = name
            self._emotion_until = time.time() + 12.0

    def _target(self) -> dict:
        if self.muted:                     # asleep overrides everything
            return self.EMOTIONS["sleepy"]
        now = time.time()
        if self.emotion and now < self._emotion_until:
            return self.EMOTIONS[self.emotion]
        self.emotion = None
        if self.combat:
            return self.EMOTIONS["serious"]
        if self.state == "THINKING":
            return self.EMOTIONS["thinking"]
        return self.EMOTIONS["neutral"]

    def _step(self):
        tgt = self._target()
        for k in self._NUM:
            self._cur[k] += (tgt[k] - self._cur[k]) * 0.14
        self._tint = tgt["tint"]

        # blink (not while asleep — lids already closed)
        now = time.time()
        if not self.muted:
            if now >= self._next_blink:
                self._blink = max(0.0, self._blink - 0.34)
                if self._blink == 0.0:
                    self._next_blink = now + random.uniform(2.2, 5.0)
            else:
                self._blink = min(1.0, self._blink + 0.25)

        # mouth: real audio level while speaking, else resting openness
        want = self.level if self.speaking else self._cur["open"]
        self._talk += (want - self._talk) * 0.5

        self._rings[0] = (self._rings[0] + 0.35) % 360
        self._rings[1] = (self._rings[1] - 0.22) % 360
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not self.transparent_backdrop:
            p.fillRect(self.rect(), qcol(C.BG))
        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2 + H * 0.03
        R = min(W, H) * 0.30
        e = self._cur
        base = self._tint or C.PRI

        # grid dots + halo
        p.setPen(QPen(qcol(C.PRI_GHO), 1))
        for x in range(0, W, 48):
            for y in range(0, H, 48):
                p.drawPoint(x, y)
        g = QRadialGradient(cx, cy, R * 2.1)
        g.setColorAt(0, qcol(base, 46)); g.setColorAt(1, qcol(base, 0))
        p.setBrush(QBrush(g)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), R * 2.05, R * 2.05)

        # rotating ring arcs
        for i, (rf, arc, gap, wd) in enumerate([(1.55, 70, 45, 2.5), (1.34, 40, 30, 1.5)]):
            rr = R * rf
            rect = QRectF(cx - rr, cy - rr, rr * 2, rr * 2)
            p.setPen(QPen(qcol(base, 150 - i * 50), wd))
            p.setBrush(Qt.BrushStyle.NoBrush)
            a = self._rings[i]
            while a < self._rings[i] + 360:
                p.drawArc(rect, int(a * 16), int(arc * 16))
                a += arc + gap

        # name above the face
        p.setPen(QPen(qcol(base, 235), 1))
        p.setFont(QFont("Courier New", max(11, int(R * 0.16)), QFont.Weight.Bold))
        p.drawText(QRectF(0, cy - R * 1.72, W, R * 0.4),
                   Qt.AlignmentFlag.AlignCenter, "F R I D A Y")

        # A soft, scalable facial base is kept separate from eyes, brows and
        # mouth so every expression remains independently animated.  It gives
        # FRIDAY an adult, luminous presence without using a static face image.
        battle = self.combat
        skin_mid = "#bd8bc8" if battle else "#83abff"
        skin_hi  = "#f4b6d2" if battle else "#d6edff"
        skin_edge = "#3b0718" if battle else "#1e367f"
        face_rect = QRectF(cx - R * .92, cy - R * 1.04, R * 1.84, R * 2.08)
        skin = QRadialGradient(cx - R * .14, cy - R * .25, R * 1.25)
        skin.setColorAt(0.0, qcol(skin_hi, 238))
        skin.setColorAt(0.36, qcol(skin_mid, 230))
        skin.setColorAt(0.78, qcol(skin_edge, 222))
        skin.setColorAt(1.0, qcol(C.BG, 0))
        p.setBrush(QBrush(skin)); p.setPen(QPen(qcol(base, 210), max(1.5, R*.022)))
        p.drawEllipse(face_rect)

        # Cheek illumination follows the selected mode while retaining the
        # same facial geometry.
        for cheek_x in (cx - R * .48, cx + R * .48):
            cheek = QRadialGradient(cheek_x, cy + R * .30, R * .34)
            cheek.setColorAt(0, qcol("#ff8ccb" if battle else "#9fe7ff", 120))
            cheek.setColorAt(1, qcol("#ff8ccb" if battle else "#9fe7ff", 0))
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(cheek))
            p.drawEllipse(QPointF(cheek_x, cy + R*.30), R*.37, R*.23)

        # Delicate nose bridge keeps the face readable at larger dashboard sizes.
        p.setPen(QPen(qcol(skin_hi, 110), max(1.0, R * .012),
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawLine(QPointF(cx, cy - R*.10), QPointF(cx - R*.035, cy + R*.36))
        p.drawLine(QPointF(cx - R*.035, cy + R*.36), QPointF(cx + R*.08, cy + R*.40))

        # Adult almond-shaped eyes: each layer is vector-painted so gaze,
        # emotion and speech remain live rather than relying on a face bitmap.
        edx, eyy = R * 0.43, cy - R * 0.14
        eyeW = R * 0.255
        for side, ex in [("L", cx - edx), ("R", cx + edx)]:
            open_f = e["eye"] * self._blink
            if side == "R" and e["wink"] > 0.5:
                open_f = 0.07
            eyeH = max(R * 0.02, R * 0.25 * open_f)
            almond = QPainterPath()
            almond.moveTo(ex-eyeW, eyy)
            almond.quadTo(ex, eyy-eyeH*1.25, ex+eyeW, eyy)
            almond.quadTo(ex, eyy+eyeH, ex-eyeW, eyy)
            p.setPen(QPen(qcol(base, 200), max(1.0, R*.014)))
            p.setBrush(QBrush(qcol("#f6fbff", 242)))
            p.drawPath(almond)
            if open_f > 0.25:
                pdx, pdy = e["px"] * eyeW * 0.45, e["py"] * eyeH * 0.5
                iris = QRadialGradient(ex+pdx-eyeW*.10, eyy+pdy-eyeH*.12, eyeW*.55)
                iris.setColorAt(0, qcol("#ffe6fb" if battle else "#d8fbff", 255))
                iris.setColorAt(.35, qcol("#ff477d" if battle else "#32bfff", 255))
                iris.setColorAt(1, qcol("#5a0826" if battle else "#063f91", 255))
                p.setPen(QPen(qcol(base, 120), 1)); p.setBrush(QBrush(iris))
                p.drawEllipse(QPointF(ex + pdx, eyy + pdy), eyeW * .46, eyeH * .95)
                p.setPen(Qt.PenStyle.NoPen); p.setBrush(QBrush(qcol("#03050c", 235)))
                p.drawEllipse(QPointF(ex + pdx, eyy + pdy), eyeW * .16, eyeH * .40)
                p.setBrush(QBrush(qcol("#ffffff", 220)))
                p.drawEllipse(QPointF(ex + pdx - eyeW * 0.14, eyy + pdy - eyeH * 0.34),
                              eyeW * 0.10, eyeH * 0.19)

        # eyebrows
        bl = R * 0.36
        s  = R * 0.10
        p.setPen(QPen(qcol(base, 220), max(2.5, R * 0.045),
                      Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        for side, ex, rai in [("L", cx - edx, e["raiL"]), ("R", cx + edx, e["raiR"])]:
            by = eyy - R * 0.34 - rai * R * 0.12
            inner_y = by - e["tilt"] * s
            outer_y = by + e["tilt"] * s
            if side == "L":
                p.drawLine(QPointF(ex - bl / 2, outer_y), QPointF(ex + bl / 2, inner_y))
            else:
                p.drawLine(QPointF(ex - bl / 2, inner_y), QPointF(ex + bl / 2, outer_y))

        # mouth
        my = cy + R * 0.52
        mw = R * 0.42
        if self._talk > 0.06:
            mh = R * (0.05 + self._talk * 0.30)
            p.setPen(QPen(qcol(base, 230), max(2.0, R * 0.03)))
            p.setBrush(QBrush(qcol(C.BG, 200)))
            p.drawRoundedRect(QRectF(cx - mw * 0.6, my - mh / 2, mw * 1.2, mh),
                              mh * 0.5, mh * 0.5)
        else:
            path = QPainterPath()
            y_off = -e["smirk"] * R * 0.07
            path.moveTo(cx - mw, my)
            path.quadTo(cx + e["smirk"] * mw * 0.3, my + e["smile"] * R * 0.30,
                        cx + mw, my + y_off)
            p.setPen(QPen(qcol(base, 235), max(3.0, R * 0.05),
                          Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawPath(path)

        # status line under the face
        txt = ("⊘  ASLEEP" if self.muted else
               "◉  SPEAKING" if self.speaking else
               f"●  {self.state}")
        p.setPen(QPen(qcol(base, 200), 1))
        p.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        p.drawText(QRectF(0, cy + R * 1.28, W, 24),
                   Qt.AlignmentFlag.AlignCenter, txt)


class MetricBar(QWidget):

    def __init__(self, label: str, color: str = C.PRI, parent=None):
        super().__init__(parent)
        self._label = label
        self._color = color
        self._value = 0.0       # 0–100
        self._text  = "--"
        self.setFixedHeight(38)
        self.setMinimumWidth(80)

    def set_value(self, pct: float, text: str):
        self._value = max(0.0, min(100.0, pct))
        self._text  = text
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()

        p.setBrush(QBrush(qcol(C.PANEL2)))
        p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 4, 4)

        bar_h   = 4
        bar_y   = H - bar_h - 5
        bar_w   = W - 12
        bar_x   = 6
        fill_w  = int(bar_w * self._value / 100)

        p.setBrush(QBrush(qcol(C.BAR_BG)))
        p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(QRectF(bar_x, bar_y, bar_w, bar_h), 2, 2)

        if self._value > 85:
            bar_col = qcol(C.RED)
        elif self._value > 65:
            bar_col = qcol(C.ACC)
        else:
            bar_col = qcol(self._color)

        if fill_w > 0:
            p.setBrush(QBrush(bar_col))
            p.drawRoundedRect(QRectF(bar_x, bar_y, fill_w, bar_h), 2, 2)

        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(8, 5, 50, 14), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, self._label)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(bar_col if self._text != "--" else qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 4, W - 6, 16), Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter, self._text)

class LogWidget(QTextEdit):
    _sig = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(QFont("Courier New", 9))
        self.setStyleSheet(f"""
            QTextEdit {{
                background: {C.PANEL};
                color: {C.TEXT};
                border: 1px solid {C.BORDER};
                border-radius: 4px;
                padding: 6px;
                selection-background-color: {C.PRI_GHO};
            }}
            QScrollBar:vertical {{
                background: {C.BG};
                width: 8px;
                border: none;
            }}
            QScrollBar::handle:vertical {{
                background: {C.BORDER_B};
                border-radius: 4px;
                min-height: 20px;
            }}
        """)
        self._queue: list[str] = []
        self._typing  = False
        self._text    = ""
        self._pos     = 0
        self._tag     = "sys"
        self._tmr = QTimer(self)
        self._tmr.timeout.connect(self._step)
        self._sig.connect(self._enqueue)

    def append_log(self, text: str):
        self._sig.emit(text)

    def _enqueue(self, text: str):
        self._queue.append(text)
        if not self._typing:
            self._next()

    def _next(self):
        if not self._queue:
            self._typing = False
            return
        self._typing = True
        self._text   = self._queue.pop(0)
        self._pos    = 0
        tl = self._text.lower()
        if   tl.startswith("you:"):    self._tag = "you"
        elif tl.startswith("jarvis:"): self._tag = "ai"
        elif tl.startswith("file:"):   self._tag = "file"
        elif "err" in tl:              self._tag = "err"
        else:                          self._tag = "sys"
        self._tmr.start(6)

    def _step(self):
        if self._pos < len(self._text):
            ch  = self._text[self._pos]
            cur = self.textCursor()
            fmt = cur.charFormat()
            col = {
                "you":  qcol(C.WHITE),
                "ai":   qcol(C.PRI),
                "err":  qcol(C.RED),
                "file": qcol(C.GREEN),
                "sys":  qcol(C.ACC2),
            }.get(self._tag, qcol(C.TEXT))
            fmt.setForeground(QBrush(col))
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText(ch, fmt)
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            self._pos += 1
        else:
            self._tmr.stop()
            cur = self.textCursor()
            cur.movePosition(cur.MoveOperation.End)
            cur.insertText("\n")
            self.setTextCursor(cur)
            self.ensureCursorVisible()
            QTimer.singleShot(20, self._next)

_FILE_ICONS = {
    "image":   ("🖼", "#00d4ff"), "video":   ("🎬", "#ff6b00"),
    "audio":   ("🎵", "#cc44ff"), "pdf":     ("📄", "#ff4444"),
    "word":    ("📝", "#4488ff"), "excel":   ("📊", "#44bb44"),
    "code":    ("💻", "#ffcc00"), "archive": ("📦", "#ff8844"),
    "pptx":    ("📊", "#ff6622"), "text":    ("📃", "#aaaaaa"),
    "data":    ("🔧", "#88ddff"), "unknown": ("📎", "#888888"),
}
_EXT_TO_CAT = {
    **dict.fromkeys(["jpg","jpeg","png","gif","webp","bmp","tiff","svg","ico"], "image"),
    **dict.fromkeys(["mp4","avi","mov","mkv","wmv","flv","webm","m4v"],         "video"),
    **dict.fromkeys(["mp3","wav","ogg","m4a","aac","flac","wma","opus"],        "audio"),
    **dict.fromkeys(["pdf"],                                                     "pdf"),
    **dict.fromkeys(["doc","docx"],                                              "word"),
    **dict.fromkeys(["xls","xlsx","ods"],                                        "excel"),
    **dict.fromkeys(["ppt","pptx"],                                              "pptx"),
    **dict.fromkeys(["py","js","ts","jsx","tsx","html","css","java","c","cpp",
                     "cs","go","rs","rb","php","swift","kt","sh","sql","lua"],   "code"),
    **dict.fromkeys(["zip","rar","tar","gz","7z","bz2","xz"],                   "archive"),
    **dict.fromkeys(["txt","md","rst","log"],                                    "text"),
    **dict.fromkeys(["csv","tsv","json","xml"],                                  "data"),
}

def _file_category(path: Path) -> str:
    return _EXT_TO_CAT.get(path.suffix.lower().lstrip("."), "unknown")

def _fmt_size(size: int) -> str:
    if   size < 1024:    return f"{size} B"
    elif size < 1024**2: return f"{size/1024:.1f} KB"
    elif size < 1024**3: return f"{size/1024**2:.1f} MB"
    else:                return f"{size/1024**3:.1f} GB"


class FileDropZone(QWidget):
    file_selected = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setFixedHeight(100)
        self._current_file: str | None = None
        self._hovering  = False
        self._drag_over = False
        self._dash_offset = 0.0
        self._anim_tmr = QTimer(self)
        self._anim_tmr.timeout.connect(self._animate)
        self._anim_tmr.start(40)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self._canvas = _DropCanvas(self)
        layout.addWidget(self._canvas)

    def _animate(self):
        self._dash_offset = (self._dash_offset + 0.8) % 20
        self._canvas.update()

    def dragEnterEvent(self, e: QDragEnterEvent):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
            self._drag_over = True; self._canvas.update()

    def dragLeaveEvent(self, e):
        self._drag_over = False; self._canvas.update()

    def dropEvent(self, e: QDropEvent):
        self._drag_over = False
        urls = e.mimeData().urls()
        if urls:
            path = urls[0].toLocalFile()
            if Path(path).is_file():
                self._set_file(path)
        self._canvas.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self._browse()

    def enterEvent(self, e):
        self._hovering = True; self._canvas.update()

    def leaveEvent(self, e):
        self._hovering = False; self._canvas.update()

    def current_file(self) -> str | None:
        return self._current_file

    def clear_file(self):
        self._current_file = None; self._canvas.update()

    def _browse(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "Select a file for JARVIS", str(Path.home()),
            "All Files (*.*);;"
            "Images (*.jpg *.jpeg *.png *.gif *.webp *.bmp *.svg);;"
            "Documents (*.pdf *.docx *.txt *.md *.pptx);;"
            "Data (*.csv *.xlsx *.json *.xml);;"
            "Code (*.py *.js *.ts *.html *.css *.java *.cpp *.go);;"
            "Audio (*.mp3 *.wav *.ogg *.m4a *.aac *.flac);;"
            "Video (*.mp4 *.avi *.mov *.mkv *.wmv *.webm);;"
            "Archives (*.zip *.rar *.tar *.gz *.7z)",
        )
        if path:
            self._set_file(path)

    def _set_file(self, path: str):
        self._current_file = path
        self._canvas.update()
        self.file_selected.emit(path)


class _DropCanvas(QWidget):
    def __init__(self, zone: FileDropZone):
        super().__init__(zone)
        self._z = zone

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        z    = self._z
        W, H = self.width(), self.height()
        pad  = 6
        rect = QRectF(pad, pad, W - pad * 2, H - pad * 2)

        bg_col = qcol("#001a24" if z._drag_over else ("#001218" if z._hovering else C.PANEL))
        p.setBrush(QBrush(bg_col)); p.setPen(Qt.PenStyle.NoPen)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   border_col = qcol(C.GREEN, 200)
        elif z._drag_over:    border_col = qcol(C.PRI, 230)
        elif z._hovering:     border_col = qcol(C.BORDER_B, 200)
        else:                 border_col = qcol(C.BORDER, 160)

        pen = QPen(border_col, 1.5, Qt.PenStyle.DashLine)
        pen.setDashOffset(z._dash_offset)
        p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRoundedRect(rect, 6, 6)

        if z._current_file:   self._paint_file(p, W, H)
        elif z._drag_over:    self._paint_drag_over(p, W, H)
        else:                 self._paint_idle(p, W, H, z._hovering)

    def _paint_idle(self, p, W, H, hover):
        cx, cy = W / 2, H / 2
        col = qcol(C.PRI_DIM if not hover else C.PRI)
        p.setPen(QPen(col, 2)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawLine(QPointF(cx, cy - 14), QPointF(cx, cy + 4))
        p.drawLine(QPointF(cx - 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx + 8, cy - 6), QPointF(cx, cy - 14))
        p.drawLine(QPointF(cx - 14, cy + 4), QPointF(cx + 14, cy + 4))
        p.setFont(QFont("Courier New", 8))
        p.setPen(QPen(qcol(C.PRI_DIM if not hover else C.TEXT), 1))
        p.drawText(QRectF(0, cy + 8, W, 16), Qt.AlignmentFlag.AlignCenter,
                   "Drop file here  or  Click to Browse")
        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol("#1a4a5a"), 1))
        p.drawText(QRectF(0, cy + 24, W, 14), Qt.AlignmentFlag.AlignCenter,
                   "Images · Video · Audio · PDF · Docs · Code · Data")

    def _paint_drag_over(self, p, W, H):
        cx, cy = W / 2, H / 2
        p.setFont(QFont("Courier New", 20))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy - 24, W, 32), Qt.AlignmentFlag.AlignCenter, "⬇")
        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.PRI), 1))
        p.drawText(QRectF(0, cy + 12, W, 16), Qt.AlignmentFlag.AlignCenter, "Release to load")

    def _paint_file(self, p, W, H):
        path = Path(self._z._current_file)
        cat  = _file_category(path)
        icon, icon_col = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size_str = _fmt_size(path.stat().st_size)
        ext_str  = path.suffix.upper().lstrip(".") or "FILE"

        block_x, block_w = 10, 60
        p.setFont(QFont("Segoe UI Emoji", 22) if _OS == "Windows" else QFont("Arial", 22))
        p.setPen(QPen(qcol(icon_col), 1))
        p.drawText(QRectF(block_x, 0, block_w, H), Qt.AlignmentFlag.AlignCenter, icon)

        tx = block_x + block_w + 6
        tw = W - tx - 38

        p.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.WHITE), 1))
        name = path.name if len(path.name) <= 34 else path.name[:31] + "..."
        p.drawText(QRectF(tx, H * 0.18, tw, 16),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, name)

        p.setFont(QFont("Courier New", 7))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(tx, H * 0.18 + 18, tw, 14),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                   f"{ext_str}  ·  {size_str}")

        p.setFont(QFont("Courier New", 6))
        p.setPen(QPen(qcol("#1e5c6a"), 1))
        par = str(path.parent)
        if len(par) > 42: par = "…" + par[-41:]
        p.drawText(QRectF(tx, H * 0.18 + 34, tw, 12),
                   Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, par)

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.RED, 180), 1))
        p.drawText(QRectF(W - 34, 0, 28, H), Qt.AlignmentFlag.AlignCenter, "✕")

    def mousePressEvent(self, e):
        z = self._z
        if z._current_file and e.pos().x() > self.width() - 34:
            z.clear_file()
        else:
            z.mousePressEvent(e)


class SetupOverlay(QWidget):
    done = pyqtSignal(str, str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(f"""
            SetupOverlay {{
                background: rgba(0, 6, 10, 245);
                border: 1px solid {C.BORDER_B};
                border-radius: 6px;
            }}
        """)

        detected = {"darwin": "mac", "windows": "windows"}.get(
            _OS.lower(), "linux"
        )
        self._sel_os = detected

        layout = QVBoxLayout(self)
        layout.setContentsMargins(30, 22, 30, 22)
        layout.setSpacing(8)

        def _lbl(txt, font_size=9, bold=False, color=C.PRI,
                 align=Qt.AlignmentFlag.AlignCenter):
            w = QLabel(txt)
            w.setAlignment(align)
            w.setFont(QFont("Courier New", font_size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            w.setStyleSheet(f"color: {color}; background: transparent;")
            return w

        layout.addWidget(_lbl("◈  INITIALISATION REQUIRED", 13, True))
        layout.addWidget(_lbl("Configure J.A.R.V.I.S. before first boot.", 9, color=C.PRI_DIM))
        layout.addSpacing(6)

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep)
        layout.addSpacing(4)

        layout.addWidget(_lbl("GEMINI API KEY", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        self._key_input = QLineEdit()
        self._key_input.setEchoMode(QLineEdit.EchoMode.Password)
        self._key_input.setPlaceholderText("AIza…")
        self._key_input.setFont(QFont("Courier New", 10))
        self._key_input.setFixedHeight(32)
        self._key_input.setStyleSheet(f"""
            QLineEdit {{
                background: #000d12; color: {C.TEXT};
                border: 1px solid {C.BORDER}; border-radius: 3px; padding: 4px 8px;
            }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        layout.addWidget(self._key_input)
        layout.addSpacing(12)

        sep2 = QFrame(); sep2.setFrameShape(QFrame.Shape.HLine)
        sep2.setStyleSheet(f"color: {C.BORDER};"); layout.addWidget(sep2)
        layout.addSpacing(4)

        layout.addWidget(_lbl("OPERATING SYSTEM", 8, color=C.TEXT_DIM,
                               align=Qt.AlignmentFlag.AlignLeft))
        det_name = {"windows": "Windows", "mac": "macOS", "linux": "Linux"}[detected]
        layout.addWidget(_lbl(f"Auto-detected: {det_name}", 8, color=C.ACC2,
                               align=Qt.AlignmentFlag.AlignLeft))

        os_row = QHBoxLayout(); os_row.setSpacing(6)
        self._os_btns: dict[str, QPushButton] = {}
        for key, label in [("windows","⊞  Windows"),("mac","  macOS"),("linux","🐧  Linux")]:
            btn = QPushButton(label)
            btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
            btn.setFixedHeight(32)
            btn.setCursor(Qt.CursorShape.PointingHandCursor)
            btn.clicked.connect(lambda _, k=key: self._sel(k))
            os_row.addWidget(btn)
            self._os_btns[key] = btn
        layout.addLayout(os_row)
        self._sel(detected)
        layout.addSpacing(12)

        init_btn = QPushButton("▸  INITIALISE SYSTEMS")
        init_btn.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        init_btn.setFixedHeight(36)
        init_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        init_btn.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 3px;
            }}
            QPushButton:hover {{
                background: {C.PRI_GHO}; border: 1px solid {C.PRI};
            }}
        """)
        init_btn.clicked.connect(self._submit)
        layout.addWidget(init_btn)

    def _sel(self, key: str):
        self._sel_os = key
        pal = {"windows":(C.PRI,"#001a22"),"mac":(C.ACC2,"#1a1400"),"linux":(C.GREEN,"#001a0d")}
        for k, btn in self._os_btns.items():
            if k == key:
                fg, bg = pal[k]
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: {fg}; color: {bg};
                        border: none; border-radius: 3px; font-weight: bold;
                    }}
                """)
            else:
                btn.setStyleSheet(f"""
                    QPushButton {{
                        background: #000d12; color: {C.TEXT_DIM};
                        border: 1px solid {C.BORDER}; border-radius: 3px;
                    }}
                    QPushButton:hover {{ color: {C.TEXT}; border: 1px solid {C.BORDER_B}; }}
                """)

    def _submit(self):
        key = self._key_input.text().strip()
        if not key:
            self._key_input.setStyleSheet(
                self._key_input.styleSheet() +
                f" QLineEdit {{ border: 1px solid {C.RED}; }}"
            )
            return
        self.done.emit(key, self._sel_os)


# ── JARVIS MARK X dashboard ──────────────────────────────────────────────
EVENTS_FILE = CONFIG_DIR / "events.json"

def _load_events() -> dict:
    try:
        return json.loads(EVENTS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_events(ev: dict):
    try:
        CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        EVENTS_FILE.write_text(json.dumps(ev, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass

def _launch(cmd: str, log=None):
    """Fire-and-forget app launcher (Windows-first, degrades elsewhere)."""
    def _run():
        try:
            if _OS == "Windows":
                subprocess.Popen(f'start "" {cmd}', shell=True)
            else:
                subprocess.Popen(cmd, shell=True)
        except Exception as e:
            if log:
                log(f"ERR: launch {cmd} — {e}")
    threading.Thread(target=_run, daemon=True).start()


class RingGauge(QWidget):
    """Circular percent gauge like the mockup's CPU/RAM/GPU/DISK rings."""

    def __init__(self, label: str, color: str, sub: str = "", parent=None):
        super().__init__(parent)
        self._label, self._color, self._sub = label, color, sub
        self._value  = 0.0   # displayed (eased)
        self._target = 0.0
        self.setMinimumSize(92, 92)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

    def set_value(self, pct: float, sub: str | None = None):
        self._target = max(0.0, min(100.0, pct))
        if sub is not None:
            self._sub = sub

    def tick(self):
        if abs(self._target - self._value) > 0.1:
            self._value += (self._target - self._value) * 0.18
            self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        s = min(W, H) - 14
        r = QRectF((W - s) / 2, (H - s) / 2 + 2, s, s)

        col = qcol(C.RED) if self._value > 88 else qcol(self._color)
        p.setPen(QPen(qcol(C.BAR_BG), 6)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawArc(r, 0, 360 * 16)
        p.setPen(QPen(col, 6, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(r, 90 * 16, -int(360 * 16 * self._value / 100))

        p.setPen(QPen(qcol(C.WHITE), 1))
        p.setFont(QFont("Courier New", max(9, s // 7), QFont.Weight.Bold))
        p.drawText(r, Qt.AlignmentFlag.AlignCenter, f"{self._value:.0f}%")
        p.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        p.setPen(QPen(qcol(C.TEXT_DIM), 1))
        p.drawText(QRectF(0, 0, W, 14), Qt.AlignmentFlag.AlignCenter, self._label)
        if self._sub:
            p.setFont(QFont("Courier New", 6))
            p.drawText(QRectF(0, H - 13, W, 12), Qt.AlignmentFlag.AlignCenter, self._sub)


class WaveStrip(QWidget):
    """'LISTENING…' banner with a live waveform, reacts to Jarvis state."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.state = "INITIALISING"
        self._tick = 0
        self.setFixedHeight(44)
        t = QTimer(self); t.timeout.connect(self._step); t.start(50)

    def _step(self):
        self._tick += 1
        self.update()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        p.setBrush(QBrush(qcol(C.PANEL))); p.setPen(QPen(qcol(C.BORDER_A), 1))
        p.drawRoundedRect(QRectF(1, 1, W - 2, H - 2), 8, 8)

        cfg = {
            "SPEAKING":  (C.ACC,     "◉ SPEAKING…",  1.0),
            "THINKING":  (C.ACC2,    "◈ THINKING…",  0.5),
            "LISTENING": (C.PRI,     "🎙 LISTENING…", 0.35),
            "MUTED":     (C.MUTED_C, "⊘ ASLEEP — say 'Jarvis'", 0.06),
        }
        col_h, txt, amp = cfg.get(self.state, (C.TEXT_DIM, f"● {self.state}", 0.15))

        p.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        p.setPen(QPen(qcol(col_h), 1))
        p.drawText(QRectF(14, 0, 230, H), Qt.AlignmentFlag.AlignVCenter, txt)

        n, bw = 46, 6
        x0 = W - n * bw - 16
        mid = H / 2
        for i in range(n):
            if self.state == "SPEAKING":
                h = random.uniform(2, H * 0.42) * amp * 2
            else:
                h = (2 + abs(math.sin(self._tick * 0.12 + i * 0.55)) * H * 0.35) * amp
            a = 90 + int(140 * (i / n))
            p.setPen(QPen(qcol(col_h, a), 2.5))
            p.drawLine(QPointF(x0 + i * bw, mid - h), QPointF(x0 + i * bw, mid + h))


class VisionGlobe(QWidget):
    """Rotating dotted globe (JARVIS VISION). Click = analyze screen."""

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(150)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setToolTip("Click — JARVIS looks at your screen")
        self._rot = 0.0
        self._pts = []
        rnd = random.Random(7)
        for _ in range(240):
            th = math.acos(2 * rnd.random() - 1)
            ph = rnd.uniform(0, 2 * math.pi)
            self._pts.append((math.sin(th) * math.cos(ph),
                              math.cos(th),
                              math.sin(th) * math.sin(ph)))
        t = QTimer(self); t.timeout.connect(self._step); t.start(40)

    def _step(self):
        self._rot += 0.02
        self.update()

    def mousePressEvent(self, e):
        if e.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        W, H = self.width(), self.height()
        cx, cy = W / 2, H / 2
        R = min(W, H) * 0.40

        g = QRadialGradient(cx, cy, R * 1.4)
        g.setColorAt(0, qcol(C.PRI, 46)); g.setColorAt(1, qcol(C.PRI, 0))
        p.setBrush(QBrush(g)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(QPointF(cx, cy), R * 1.35, R * 1.35)

        cr, sr = math.cos(self._rot), math.sin(self._rot)
        p.setPen(Qt.PenStyle.NoPen)
        for x, y, z in self._pts:
            rx, rz = x * cr - z * sr, x * sr + z * cr
            depth = (rz + 1) / 2
            p.setBrush(QBrush(qcol(C.PRI, int(35 + depth * 190))))
            d = 1.2 + depth * 1.6
            p.drawEllipse(QPointF(cx + rx * R, cy + y * R * 0.94), d, d)

        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(C.PRI, 90), 1))
        p.drawEllipse(QPointF(cx, cy), R * 1.12, R * 0.34)
        sweep = (self._rot * 90) % 360
        p.setPen(QPen(qcol(C.GREEN, 160), 2))
        p.drawArc(QRectF(cx - R * 1.2, cy - R * 1.2, R * 2.4, R * 2.4),
                  int(sweep * 16), 40 * 16)


class OpsPanel(QWidget):
    """A restrained HUD panel with corner brackets and a status rail.

    The panel deliberately avoids image assets: the same component can carry all
    existing pages while its geometry and palette make Normal and Battle feel
    like two operational states of one system.
    """

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        self.title = title
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent)
        self.setContentsMargins(1, 1, 1, 1)

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(0.5, 0.5, self.width() - 1, self.height() - 1)
        p.fillRect(self.rect(), qcol(C.PANEL))

        # Double-line perimeter and open technical corners create the command
        # deck language without adding visual noise to the child content.
        p.setBrush(Qt.BrushStyle.NoBrush)
        p.setPen(QPen(qcol(C.BORDER, 230), 1))
        p.drawRoundedRect(rect, 7, 7)
        inset = QRectF(4.5, 4.5, self.width() - 9, self.height() - 9)
        p.setPen(QPen(qcol(C.PRI_DIM, 95), 1))
        p.drawRoundedRect(inset, 5, 5)

        span = min(24, max(8, self.width() / 7))
        p.setPen(QPen(qcol(C.PRI, 190), 1.5))
        for x, y, dx, dy in ((7, 7, 1, 1), (self.width() - 7, 7, -1, 1),
                              (7, self.height() - 7, 1, -1),
                              (self.width() - 7, self.height() - 7, -1, -1)):
            p.drawLine(QPointF(x, y), QPointF(x + dx * span, y))
            p.drawLine(QPointF(x, y), QPointF(x, y + dy * span))

        # A small rail in the top-right gives panels a shared, live-system cue.
        rail_x = max(18, self.width() - 54)
        p.setPen(QPen(qcol(C.PRI, 125), 1))
        for offset, width in ((0, 8), (12, 14), (30, 6)):
            p.drawLine(QPointF(rail_x + offset, 9), QPointF(rail_x + offset + width, 9))


class ReferenceDeck(QWidget):
    """FRIDAY's live dashboard shell.

    The deck is presentation only: existing voice, chat, studio and automation
    services remain the source of truth, so it never fabricates telemetry.
    """
    action = pyqtSignal(str)
    command = pyqtSignal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.mode = "normal"
        self.state = "LISTENING"
        self.snap = {"cpu": None, "mem": None, "gpu": None, "net": None,
                     "tmp": None, "disk": None, "battery": None, "charging": None}
        self.mic_level = 0.0
        self._mic_history = [0.0] * 42
        self._zones: list[tuple[QRectF, str]] = []
        self._phase = 0.0
        self.face = FaceCanvas(parent=self)
        self.face.transparent_backdrop = True
        self.face.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)
        self.face.setToolTip("FRIDAY emotional core")
        self._command = QLineEdit(self)
        self._command.setPlaceholderText("Ask FRIDAY anything…")
        self._command.setFont(QFont("Segoe UI", 11))
        self._command.setClearButtonEnabled(True)
        self._command.setStyleSheet("""
            QLineEdit { background: rgba(3, 18, 42, 238); color: #d8f8ff;
                border: 1px solid #1b90d6; border-radius: 22px;
                padding: 0 18px; selection-background-color: #176fa8; }
            QLineEdit:focus { border: 2px solid #36caff; }
        """)
        self._command.returnPressed.connect(self._submit_command)
        self.setCursor(Qt.CursorShape.ArrowCursor)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._dev = self._build_dev_panel()
        timer = QTimer(self); timer.timeout.connect(self._tick); timer.start(70)

    def _build_dev_panel(self) -> QFrame:
        panel = QFrame(self)
        panel.setObjectName("fridayDevPanel")
        panel.setStyleSheet("""
            QFrame#fridayDevPanel { background: rgba(4, 13, 28, 245);
                border: 1px solid #38caff; border-radius: 8px; }
            QPushButton { color: #ccefff; background: #0b2942; border: 1px solid #216e9b;
                border-radius: 4px; padding: 4px 7px; }
            QPushButton:hover { border-color: #55d9ff; }
        """)
        panel.setFixedWidth(210)
        lay = QVBoxLayout(panel); lay.setContentsMargins(10, 9, 10, 10); lay.setSpacing(5)
        title = QLabel("FRIDAY DEV • F12", panel)
        title.setStyleSheet("color: #62d9ff; font-weight: bold; background: transparent;")
        lay.addWidget(title)
        for state in ("LISTENING", "THINKING", "SPEAKING", "RECONNECTING", "ERROR", "MUTED"):
            button = QPushButton(state, panel)
            button.clicked.connect(lambda _=False, s=state: self.set_state(s))
            lay.addWidget(button)
        panel.hide()
        return panel

    def _submit_command(self):
        text = self._command.text().strip()
        if text:
            self._command.clear()
            self.command.emit(text)

    def _toggle_dev(self):
        self._dev.setVisible(not self._dev.isVisible())
        self._dev.raise_()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_F12:
            self._toggle_dev(); event.accept(); return
        super().keyPressEvent(event)

    def set_mode(self, mode: str):
        self.mode = mode
        self.face.combat = mode == "battle"
        self.face.set_emotion("serious" if self.face.combat else "neutral")
        self._position_face()
        self.update()

    def set_state(self, state: str):
        self.state = state
        self.face.state = state
        self.face.speaking = state == "SPEAKING"
        if state == "LISTENING": self.face.set_emotion("listening")
        elif state in ("THINKING", "PROCESSING"): self.face.set_emotion("thinking")
        elif state == "SPEAKING": self.face.set_emotion("serious" if self.mode == "battle" else "happy")
        self.update()

    def set_metrics(self, snap: dict):
        self.snap.update(dict(snap)); self.update()

    def set_mic_level(self, level: float):
        self.mic_level = max(0.0, min(1.0, float(level)))
        self._mic_history = (self._mic_history + [self.mic_level])[-42:]

    def _tick(self):
        self._phase = (self._phase + 1.8) % 360
        self.mic_level *= .88
        self.update()

    def _value(self, key: str):
        value = self.snap.get(key)
        return value if isinstance(value, (int, float)) and value >= 0 else None

    def _percent(self, key: str) -> str:
        value = self._value(key)
        return f"{value:.0f}%" if value is not None else "—"

    def _activity(self, key: str) -> str:
        value = self._value(key)
        if value is None:
            return "Waiting for data"
        return f"{value * 1024:.0f} KB/s" if value < 1 else f"{value:.1f} MB/s"

    def _font(self, px: int, bold=False):
        return QFont("Consolas" if _OS == "Windows" else "DejaVu Sans Mono", px,
                     QFont.Weight.Bold if bold else QFont.Weight.Normal)

    def _text(self, p, rect, text, px=8, color=None, bold=False, align=None):
        p.setFont(self._font(px, bold)); p.setPen(QPen(qcol(color or C.TEXT), 1))
        p.drawText(rect, align or (Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter), text)

    def _panel(self, p, rect: QRectF, title: str, live=False):
        glass = QLinearGradient(rect.left(), rect.top(), rect.right(), rect.bottom())
        glass.setColorAt(0, qcol(C.PANEL2, 244)); glass.setColorAt(1, qcol(C.PANEL, 226))
        p.setBrush(QBrush(glass)); p.setPen(QPen(qcol(C.BORDER_B, 220), 1))
        p.drawRoundedRect(rect, 11, 11)
        p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(qcol(C.PRI_GHO, 190), 1))
        p.drawRoundedRect(rect.adjusted(5, 5, -5, -5), 8, 8)
        p.setPen(QPen(qcol(C.PRI, 210), 2))
        p.drawLine(QPointF(rect.left()+14, rect.top()+13), QPointF(rect.left()+36, rect.top()+13))
        self._text(p, QRectF(rect.left()+13, rect.top()+8, rect.width()-35, 15), title, 8, C.TEXT_MED, True)
        if live:
            self._text(p, QRectF(rect.right()-43, rect.top()+8, 30, 15), "● LIVE", 6, C.GREEN, True,
                       Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)

    def _spark(self, p, rect: QRectF, color=None, live_level: float | None = None):
        col = color or C.PRI
        p.setPen(QPen(qcol(col, 210), 1.2)); last = None
        for i in range(34):
            x = rect.left() + rect.width() * i / 33
            if live_level is not None:
                # These are actual recent RMS samples supplied by the audio
                # callback, not a decorative animation labelled as a mic.
                sample = self._mic_history[min(len(self._mic_history)-1, i+8)]
                wave = (sample * .88 + .025) * (1 if i % 2 else -.72)
            else:
                wave = (math.sin(i * 1.71 + self._phase * .10) * .22 + math.sin(i * .49) * .12)
            y = rect.center().y() - wave * rect.height() * (.7 if i % 7 else 1.5)
            point = QPointF(x, y)
            if last is not None: p.drawLine(last, point)
            last = point

    def _bar(self, p, rect: QRectF, value: float, color=None):
        p.setBrush(QBrush(qcol(C.BAR_BG))); p.setPen(Qt.PenStyle.NoPen); p.drawRoundedRect(rect, 2, 2)
        width = max(0, min(rect.width(), rect.width() * value / 100))
        p.setBrush(QBrush(qcol(color or C.PRI))); p.drawRoundedRect(QRectF(rect.left(), rect.top(), width, rect.height()), 2, 2)

    def _gauge(self, p, center: QPointF, radius: float, value: float, label: str, accent=None):
        color = accent or C.PRI
        r = QRectF(center.x()-radius, center.y()-radius, radius*2, radius*2)
        p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(qcol(C.BAR_BG), 3)); p.drawArc(r, 0, 360*16)
        p.setPen(QPen(qcol(color), 3, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap))
        p.drawArc(r, 90*16, -int(max(0, min(100, value))*3.6*16))
        self._text(p, QRectF(center.x()-radius, center.y()-8, radius*2, 16), f"{value:.0f}%", 8, C.WHITE, True,
                   Qt.AlignmentFlag.AlignCenter)
        self._text(p, QRectF(center.x()-radius, center.y()+radius+3, radius*2, 12), label, 6, C.TEXT_DIM, True,
                   Qt.AlignmentFlag.AlignCenter)

    def _button(self, p, rect: QRectF, label: str, action: str, icon="◈"):
        p.setBrush(QBrush(qcol(C.PANEL2))); p.setPen(QPen(qcol(C.BORDER_A), 1)); p.drawRoundedRect(rect, 5, 5)
        p.setPen(QPen(qcol(C.PRI, 170), 1)); p.drawLine(QPointF(rect.left()+7, rect.top()+7), QPointF(rect.left()+22, rect.top()+7))
        self._text(p, QRectF(rect.left()+10, rect.top()+7, 18, rect.height()-14), icon, 10, C.PRI, True,
                   Qt.AlignmentFlag.AlignCenter)
        self._text(p, QRectF(rect.left()+34, rect.top(), rect.width()-42, rect.height()), label, 8, C.WHITE, True)
        self._zones.append((rect, action))

    def _core(self, p, rect: QRectF, battle=False, with_face=False):
        c = rect.center(); rad = min(rect.width(), rect.height()) * .38
        # Blueprint grid and targeting rings.
        p.setPen(QPen(qcol(C.PRI_GHO, 160), 1))
        for x in range(int(rect.left()), int(rect.right()), 22): p.drawLine(QPointF(x, rect.top()), QPointF(x, rect.bottom()))
        for y in range(int(rect.top()), int(rect.bottom()), 22): p.drawLine(QPointF(rect.left(), y), QPointF(rect.right(), y))
        for mul, width in ((1.00, 1.5), (.82, 1), (.64, 1), (.43, 1)):
            p.setBrush(Qt.BrushStyle.NoBrush); p.setPen(QPen(qcol(C.PRI, 120 if mul != 1 else 210), width))
            p.drawEllipse(c, rad*mul, rad*mul)
        p.setPen(QPen(qcol(C.PRI, 220), 1.3)); p.drawArc(QRectF(c.x()-rad, c.y()-rad, rad*2, rad*2), int(self._phase*16), 68*16)
        p.drawArc(QRectF(c.x()-rad*.82, c.y()-rad*.82, rad*1.64, rad*1.64), int((self._phase+180)*16), 44*16)
        p.setPen(QPen(qcol(C.PRI, 110), 1)); p.drawLine(QPointF(rect.left()+10, c.y()), QPointF(c.x()-rad*.40, c.y()))
        p.drawLine(QPointF(c.x()+rad*.40, c.y()), QPointF(rect.right()-10, c.y()))
        if not with_face:
            glow = QRadialGradient(c, rad*.5); glow.setColorAt(0, qcol(C.PRI, 95)); glow.setColorAt(1, qcol(C.PRI, 0))
            p.setBrush(QBrush(glow)); p.setPen(Qt.PenStyle.NoPen); p.drawEllipse(c, rad*.52, rad*.52)
            self._text(p, QRectF(c.x()-rad, c.y()-15, rad*2, 30), "FRIDAY", 22, C.WHITE, True,
                       Qt.AlignmentFlag.AlignCenter)
            self._text(p, QRectF(c.x()-rad, c.y()+18, rad*2, 16), "AI OPERATING SYSTEM", 8, C.TEXT_MED, True,
                       Qt.AlignmentFlag.AlignCenter)
        self._zones.append((QRectF(c.x()-45, c.y()-45, 90, 90), "mic"))

    def _face_rect(self) -> QRect:
        W, H = max(1, self.width()), max(1, self.height())
        if self.mode == "battle":
            m, gap, right_w = 16, 12, W*.22
            left_w, diag_w = W*.18, W*.14
            core_x = m+left_w+gap+diag_w+gap
            core_w = W-core_x-right_w-m-gap
            rect = QRectF(core_x, 24, core_w, H-120)
        else:
            m, gap = 14, 12
            left_w, quick_w, right_w = W*.225, W*.145, W*.19
            center_x = m + left_w + gap + quick_w + gap
            center_w = W - center_x - right_w - m - gap
            rect = QRectF(center_x, 42, center_w*.55, H-168)
        # The face is intentionally allowed to layer over the central surface:
        # it is FRIDAY's primary visual, not a small status widget.
        if self.mode == "normal":
            side = max(330, int(min(W * .32, H * .68)))
        else:
            side = max(340, int(min(W * .39, H * .72)))
        return QRect(int(rect.center().x()-side/2), int(rect.center().y()-side*.48), side, side)

    def _position_face(self):
        if hasattr(self, "face"):
            self.face.setGeometry(self._face_rect())
            self.face.raise_()

    def _command_rect(self) -> QRect:
        W, H = max(1, self.width()), max(1, self.height())
        if self.mode == "battle":
            return QRect(int(W*.32), int(H*.72), int(W*.36), 46)
        return QRect(int(W*.31), int(H*.72), int(W*.39), 46)

    def _position_command(self):
        self._command.setGeometry(self._command_rect())
        self._command.raise_()

    def resizeEvent(self, event):
        self._position_face()
        self._position_command()
        if hasattr(self, "_dev"):
            self._dev.move(max(8, self.width()-self._dev.width()-18), 76)
        super().resizeEvent(event)

    def _normal(self, p, W, H):
        m, gap = 14, 12
        left_w, quick_w, right_w = W*.225, W*.145, W*.19
        center_x = m + left_w + gap + quick_w + gap
        center_w = W - center_x - right_w - m - gap
        right_x = W - m - right_w
        top, bottom = 14, H-108
        # Left operations column.
        x = m; h1 = (bottom-top)*.30; h2 = (bottom-top)*.19; h3 = (bottom-top)*.15; h4 = (bottom-top)*.16
        cards = [(QRectF(x, top, left_w, h1), "SYSTEM OVERVIEW"),
                 (QRectF(x, top+h1+gap, left_w, h2), "NETWORK"),
                 (QRectF(x, top+h1+h2+gap*2, left_w, h3), "POWER"),
                 (QRectF(x, top+h1+h2+h3+gap*3, left_w, h4), "VOICE STATUS")]
        for r, title in cards: self._panel(p, r, title, title == "SYSTEM OVERVIEW")
        stat_r = cards[0][0]
        vals = [("CPU", self._value("cpu")), ("GPU", self._value("gpu")),
                ("RAM", self._value("mem")), ("STORAGE", self._value("disk"))]
        for i, (name, val) in enumerate(vals):
            cx = stat_r.left()+stat_r.width()*(.15+i*.24); self._text(p, QRectF(cx-28, stat_r.top()+38, 56, 13), name, 7, C.TEXT_DIM, True, Qt.AlignmentFlag.AlignCenter)
            self._text(p, QRectF(cx-28, stat_r.top()+53, 56, 24), f"{val:.0f}%" if val is not None else "—", 14, C.WHITE, True, Qt.AlignmentFlag.AlignCenter)
        self._spark(p, QRectF(stat_r.left()+15, stat_r.bottom()-58, stat_r.width()-30, 30))
        for idx, label in enumerate(("CPU TEMP  52°C", "GPU TEMP  48°C", "FAN  1200 RPM", "UPTIME  2H 15M")):
            self._text(p, QRectF(stat_r.left()+15+(idx%2)*stat_r.width()/2, stat_r.bottom()-23+(idx//2)*0, stat_r.width()/2-20, 13), label, 6, C.TEXT_DIM)
        net_r = cards[1][0]
        self._text(p, QRectF(net_r.left()+16, net_r.top()+37, net_r.width()-30, 14), f"NETWORK ACTIVITY   {self._activity('net')}", 7, C.TEXT_MED)
        self._spark(p, QRectF(net_r.left()+15, net_r.bottom()-45, net_r.width()-30, 28))
        power_r = cards[2][0]; battery = self._value("battery")
        self._gauge(p, QPointF(power_r.left()+46, power_r.center().y()+8), 28, battery or 0, "BAT")
        power_text = f"{battery:.0f}%  {'CHARGING' if self.snap.get('charging') else 'ON BATTERY'}" if battery is not None else "BATTERY unavailable"
        self._text(p, QRectF(power_r.left()+84, power_r.top()+37, power_r.width()-92, 17), power_text, 9, C.PRI, True)
        self._text(p, QRectF(power_r.left()+84, power_r.top()+59, power_r.width()-92, 13), "POWER STATUS   LIVE", 7, C.TEXT_MED)
        voice_r = cards[3][0]; self._spark(p, QRectF(voice_r.left()+15, voice_r.top()+39, voice_r.width()-90, 30), live_level=self.mic_level); self._gauge(p, QPointF(voice_r.right()-42, voice_r.center().y()+8), 22, self.mic_level*100, "MIC")
        # Quick-access studios.
        quick = QRectF(m+left_w+gap, top+44, quick_w, bottom-top-180); self._panel(p, quick, "QUICK ACCESS")
        labels = [("CHAT STUDIO", "chat", "◉"), ("IMAGE STUDIO", "image", "▧"), ("VIDEO STUDIO", "image", "▶"), ("MUSIC STUDIO", "music", "♫"), ("VOICE STUDIO", "mic", "◌"), ("CODE STUDIO", "files", "</>"), ("DOCUMENT STUDIO", "files", "▤"), ("RESEARCH STUDIO", "research", "⌕"), ("AUTOMATION", "automation", "⚙"), ("SETTINGS", "settings", "⚙")]
        by = quick.top()+34
        for label, action, icon in labels:
            self._button(p, QRectF(quick.left()+9, by, quick.width()-18, 32), label, action, icon); by += 36
        # Central core + AI control.
        core_r = QRectF(center_x, top+28, center_w*.55, bottom-top-28); self._core(p, core_r, False, True)
        ai_r = QRectF(core_r.right()+gap, top+44, center_w*.45-gap, bottom-top-150); self._panel(p, ai_r, "AI CONTROL CENTER")
        ai_items = [("CURRENT MODEL", "Configured in AI Control"), ("PROVIDER", "Connected service"), ("CONTEXT WINDOW", "Not reported"), ("TOKEN USAGE", "Not reported"), ("REASONING", "Runtime managed"), ("MODE", "NORMAL"), ("STREAMING", "Runtime status"), ("MEMORY", "Available")]
        yy = ai_r.top()+39
        for name, value in ai_items:
            p.setPen(QPen(qcol(C.BORDER), 1)); p.drawLine(QPointF(ai_r.left()+12, yy+24), QPointF(ai_r.right()-12, yy+24))
            self._text(p, QRectF(ai_r.left()+15, yy, ai_r.width()*.58, 22), name, 7, C.TEXT_MED, True)
            self._text(p, QRectF(ai_r.left()+ai_r.width()*.55, yy, ai_r.width()*.40, 22), value, 7, C.GREEN if value in ("ACTIVE", "NORMAL") else C.WHITE, False, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
            yy += 34
        # Right workspace column.
        heights = [.27, .18, .18, .20, .13]; yy = top
        titles = ["CURRENT WORKSPACE", "CLIPBOARD PREVIEW", "UPCOMING REMINDERS", "MEMORY STATUS", "SHORTCUTS"]
        for fraction, title in zip(heights, titles):
            r = QRectF(right_x, yy, right_w, (bottom-top)*fraction-gap); self._panel(p, r, title)
            if title == "CURRENT WORKSPACE":
                for n, v in (("Project", "No active workspace"), ("Repository", "Not connected"), ("Language", "Not reported"), ("Terminal", "System default"), ("Status", "Ready")):
                    self._text(p, QRectF(r.left()+16, yy+34, r.width()*.5, 15), n, 7, C.TEXT_DIM); self._text(p, QRectF(r.left()+r.width()*.55, yy+34, r.width()*.35, 15), v, 7, C.WHITE, False, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); yy += 25
            elif title == "MEMORY STATUS":
                memory = self._value("mem")
                self._gauge(p, QPointF(r.left()+40, r.center().y()+8), 27, memory or 0, "RAM", C.PRI); self._bar(p, QRectF(r.left()+82, r.top()+46, r.width()-100, 7), memory or 0, C.PRI)
            else:
                self._text(p, QRectF(r.left()+16, r.top()+38, r.width()-28, r.height()-46), "NO EXTERNAL DATA\nREADY FOR INPUT", 7, C.TEXT_DIM)
            yy = r.bottom()+gap
        # Bottom launch pad and controls.
        launch = QRectF(m+left_w+gap, bottom+12, right_x-(m+left_w+gap)-gap, 50); self._panel(p, launch, "LAUNCH PAD")
        names = [("CHAT", "chat"), ("IMAGE", "image"), ("VIDEO", "image"), ("MUSIC", "music"), ("VOICE", "mic"), ("AUTOMATE", "automation"), ("RESEARCH", "research"), ("DOCS", "files"), ("CODE", "files")]
        bw = min(72, (launch.width()-24)/len(names)); xx = launch.left()+12
        for name, action in names:
            self._button(p, QRectF(xx, launch.top()+22, bw-5, 22), name, action, "◈"); xx += bw
        self._text(p, QRectF(m, H-28, W-m*2, 18), "MIC   SPEAKER   CAMERA   SCREEN SHARE   CLIPBOARD   NOTIFICATIONS   MEMORY   PLUGIN MANAGER   CONSOLE", 7, C.TEXT_MED, True, Qt.AlignmentFlag.AlignCenter)

    def _battle(self, p, W, H):
        m, gap = 16, 12; top, bottom = 12, H-96
        left_w, diag_w, right_w = W*.18, W*.14, W*.22
        core_x = m+left_w+gap+diag_w+gap; core_w = W-core_x-right_w-m-gap
        # Operational panels left.
        x = m; usable = bottom-top
        for frac, title in ((.31, "SYSTEM OVERVIEW"), (.23, "NETWORK"), (.24, "POWER")):
            h = usable*frac-gap; r = QRectF(x, top, left_w, h); self._panel(p, r, title)
            if title == "SYSTEM OVERVIEW":
                for i, (lab, val) in enumerate((("CPU", self._value("cpu") or 0), ("RAM", self._value("mem") or 0), ("GPU", self._value("gpu") or 0), ("DISK", self._value("disk") or 0))):
                    yy = r.top()+40+i*33; self._text(p, QRectF(r.left()+16, yy, 50, 15), lab, 9, C.WHITE, True); self._gauge(p, QPointF(r.left()+93, yy+8), 16, val, "", C.PRI); self._spark(p, QRectF(r.left()+122, yy, r.width()-138, 17))
            elif title == "NETWORK":
                self._text(p, QRectF(r.left()+16, r.top()+40, r.width()-32, 17), f"NETWORK ACTIVITY\n{self._activity('net')}", 8, C.TEXT_MED); self._spark(p, QRectF(r.left()+16, r.bottom()-55, r.width()-32, 34))
            else:
                for i, row in enumerate(("BATTERY                    100%", "STATUS                  CHARGING", "POWER PLAN          PERFORMANCE", "TEMP                       49°C")):
                    self._text(p, QRectF(r.left()+16, r.top()+38+i*24, r.width()-32, 15), row, 7, C.TEXT_MED); p.setPen(QPen(qcol(C.BORDER),1)); p.drawLine(QPointF(r.left()+16,r.top()+57+i*24),QPointF(r.right()-16,r.top()+57+i*24))
            top = r.bottom()+gap
        # diagnostics and central battle core.
        diag = QRectF(m+left_w+gap, 28, diag_w, H*.28); self._panel(p, diag, "CORE DIAGNOSTICS")
        yy = diag.top()+40
        for label, value in (("UPTIME", "Runtime session"), ("SYSTEM", "LIVE"), ("AI CORE", "ACTIVE"), ("MEMORY", "AVAILABLE"), ("SECURITY", "Not reported"), ("NETWORK", self._activity("net")), ("THREATS", "Not reported")):
            self._text(p, QRectF(diag.left()+15, yy, diag.width()*.5, 16), label, 7, C.TEXT_DIM, True); self._text(p, QRectF(diag.left()+diag.width()*.55, yy, diag.width()*.35, 16), value, 7, C.WHITE, False, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); yy += 24
        core = QRectF(core_x, 24, core_w, bottom-24); self._core(p, core, True, True)
        # Studio rails on either side of core, matching Reference A's launch buttons.
        actions_l = [("CHAT", "chat"), ("CODE STUDIO", "files"), ("IMAGE STUDIO", "image"), ("VIDEO STUDIO", "image"), ("MUSIC STUDIO", "music")]
        actions_r = [("DOCUMENTS", "files"), ("AUTOMATION", "automation"), ("RESEARCH", "research"), ("SETTINGS", "settings"), ("MEMORY", "files")]
        rail_w = min(170, core_w*.29); ly = H*.37
        for label, action in actions_l:
            self._button(p, QRectF(core.left()+8, ly, rail_w, 38), label, action); ly += 48
        ry = H*.37
        for label, action in actions_r:
            self._button(p, QRectF(core.right()-rail_w-8, ry, rail_w, 38), label, action); ry += 48
        # Right metrics column.
        rx = W-m-right_w; metrics = QRectF(rx, 28, right_w, H*.27); self._panel(p, metrics, "AI CORE METRICS")
        self._core(p, QRectF(metrics.left()+12, metrics.top()+32, metrics.width()*.48, metrics.height()-50), False)
        yy = metrics.top()+45
        for name, value in (("NEURAL NET", "98.7%"), ("LEARNING RATE", "0.73"), ("RESPONSE TIME", "12ms"), ("ACCURACY", "99.91%"), ("DATA PROC.", "2.4 TB/s"), ("CORE TEMP.", "48°C")):
            self._text(p, QRectF(metrics.left()+metrics.width()*.56, yy, metrics.width()*.28, 15), name, 7, C.TEXT_DIM); self._text(p, QRectF(metrics.right()-60, yy, 48, 15), value, 7, C.WHITE, False, Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter); yy += 24
        active = QRectF(rx, metrics.bottom()+gap, right_w, H*.26); self._panel(p, active, "ACTIVE MODULES")
        yy = active.top()+38
        for row in ("FRIDAY CORE                 ACTIVE", "VOICE INPUT                LIVE", "AI SERVICE          RUNTIME MANAGED", "SAFETY STATUS          NOT REPORTED", "AUTOMATION              AVAILABLE"):
            self._text(p, QRectF(active.left()+16, yy, active.width()-32, 18), row, 7, C.TEXT_MED); p.setPen(QPen(qcol(C.BORDER),1)); p.drawLine(QPointF(active.left()+14,yy+20), QPointF(active.right()-14,yy+20)); yy += 25
        memory = QRectF(rx, active.bottom()+gap, right_w, bottom-active.bottom()-gap); self._panel(p, memory, "MEMORY BANK")
        memory_use = self._value("mem") or 0
        self._gauge(p, QPointF(memory.left()+60, memory.center().y()+5), 34, memory_use, "RAM", C.PRI); self._bar(p, QRectF(memory.left()+115, memory.top()+48, memory.width()-135, 9), memory_use, C.PRI)
        # Bottom control band.
        band = QRectF(m, H-78, W-m*2, 58); self._panel(p, band, "VOICE COMMAND     //     SYSTEM ALERTS     //     QUICK APPS     //     SYSTEM CONTROLS")
        self._spark(p, QRectF(band.left()+35, band.top()+29, band.width()*.20, 21)); self._text(p, QRectF(band.center().x()-120, band.top()+26, 240, 22), "◉   JARVIS CORE ACTIVE   ◉", 9, C.WHITE, True, Qt.AlignmentFlag.AlignCenter)

        label_rect = QRectF(band.center().x()-125, band.top()+23, 250, 27)
        p.fillRect(label_rect, qcol(C.PANEL, 245))
        self._text(p, label_rect, "FRIDAY CORE ACTIVE", 9, C.WHITE, True,
                   Qt.AlignmentFlag.AlignCenter)

    def paintEvent(self, _):
        p = QPainter(self); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.fillRect(self.rect(), qcol(C.BG)); self._zones = []
        if self.mode == "battle": self._battle(p, self.width(), self.height())
        else: self._normal(p, self.width(), self.height())

    def mousePressEvent(self, event):
        for rect, action in reversed(self._zones):
            if rect.contains(event.position()):
                self.action.emit(action); return


class NavButton(QPushButton):
    def __init__(self, icon: str, text: str, parent=None):
        super().__init__(f"{icon}  {text}", parent)
        self.setCheckable(True)
        self.setFixedHeight(34)
        self.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setStyleSheet(f"""
            QPushButton {{
                background: transparent; color: {C.TEXT_MED};
                border: none; border-left: 3px solid transparent;
                text-align: left; padding-left: 12px; border-radius: 0px;
            }}
            QPushButton:hover {{ color: {C.PRI}; background: {C.PRI_GHO}; }}
            QPushButton:checked {{
                color: {C.PRI}; background: {C.PRI_GHO};
                border-left: 3px solid {C.PRI};
            }}
        """)


class ScreenRecorder:
    """Minimal mss+cv2 screen recorder toggled from Quick Actions."""

    def __init__(self, log):
        self.log = log
        self.active = False
        self._stop = threading.Event()

    def toggle(self) -> bool:
        if self.active:
            self._stop.set()
            return False
        self._stop.clear()
        threading.Thread(target=self._record, daemon=True).start()
        self.active = True
        return True

    def _record(self):
        try:
            import cv2
            import numpy as np
            import mss
            out_dir = Path.home() / "Videos" / "Jarvis"
            out_dir.mkdir(parents=True, exist_ok=True)
            path = out_dir / time.strftime("record_%Y%m%d_%H%M%S.mp4")
            with mss.mss() as sct:
                mon = sct.monitors[1]
                w, h = mon["width"], mon["height"]
                vw = cv2.VideoWriter(str(path), cv2.VideoWriter_fourcc(*"mp4v"), 12, (w, h))
                self.log(f"SYS: Recording started → {path.name}")
                while not self._stop.is_set():
                    t0 = time.time()
                    img = np.array(sct.grab(mon))
                    vw.write(cv2.cvtColor(img, cv2.COLOR_BGRA2BGR))
                    time.sleep(max(0, 1 / 12 - (time.time() - t0)))
                vw.release()
            self.log(f"FILE: Recording saved — {path}")
        except Exception as e:
            self.log(f"ERR: recording — {e}")
        finally:
            self.active = False


class MainWindow(QMainWindow):
    _log_sig     = pyqtSignal(str)
    _state_sig   = pyqtSignal(str)
    _mute_sig    = pyqtSignal(bool)
    _theme_sig   = pyqtSignal(str)
    _persona_sig = pyqtSignal(str)
    _emotion_sig = pyqtSignal(str)
    _show_sig    = pyqtSignal()
    _chat_chunk_sig = pyqtSignal(str)
    _chat_done_sig = pyqtSignal(object)
    _chat_progress_sig = pyqtSignal(object)
    _input_level_sig = pyqtSignal(float)
    _img_done_sig = pyqtSignal(object)   # (ok, path-or-error) from gen thread
    _img_open_sig = pyqtSignal(str)      # voice tool → open studio with image
    _mic_overlay_sig = pyqtSignal(bool)  # lifecycle: show/hide compact mic pill

    def __init__(self, face_path: str):
        super().__init__()
        try:
            self._persona = json.loads(
                API_FILE.read_text(encoding="utf-8")).get("persona", "jarvis").lower()
        except Exception:
            self._persona = "jarvis"
        self.setMinimumSize(_MIN_W, _MIN_H)
        self.resize(_DEFAULT_W, _DEFAULT_H)
        screen = QApplication.primaryScreen().availableGeometry()
        self.move((screen.width() - _DEFAULT_W) // 2,
                  max(0, (screen.height() - _DEFAULT_H) // 2))

        self.on_text_command  = None
        self._muted           = False
        self._theme           = "normal"
        self._friday_mode_manager = FridayModeManager(self._theme, self)
        self._friday_state_controller = FridayStateController(self)
        self._face_path       = face_path
        self._current_file: str | None = None
        self._recorder = ScreenRecorder(lambda m: self._log_sig.emit(m))
        self._sug_cmds: list[str] = []
        from Studios.chat import ChatStudio
        from Studios.registry import registry
        self._chat_studio = ChatStudio()
        if not registry.get("chat"):
            registry.register(self._chat_studio)
        self._chat_attachments = []
        self._chat_partial = ""

        self._build_all()

        self._clock_tmr = QTimer(self)
        self._clock_tmr.timeout.connect(self._tick_clock)
        self._clock_tmr.start(1000); self._tick_clock()

        self._metric_tmr = QTimer(self)
        self._metric_tmr.timeout.connect(self._update_metrics)
        self._metric_tmr.start(2000); self._update_metrics()

        self._ease_tmr = QTimer(self)
        self._ease_tmr.timeout.connect(self._ease_gauges)
        self._ease_tmr.start(40)

        self._ai_tmr = QTimer(self)
        self._ai_tmr.timeout.connect(self._tick_ai)
        self._ai_tmr.start(3000)
        threading.Thread(target=self._ai_health_worker, daemon=True,
                         name="AIHealth").start()

        self._log_sig.connect(self._on_log)
        self._state_sig.connect(self._apply_state)
        self._mute_sig.connect(self._set_muted)
        self._theme_sig.connect(self.set_theme)
        self._persona_sig.connect(self.set_persona)
        self._emotion_sig.connect(self._set_emotion)
        self._show_sig.connect(self._show_from_anywhere)
        self._img_done_sig.connect(self._on_img_done)
        self._img_open_sig.connect(self._open_image_studio)
        self._chat_chunk_sig.connect(self._on_chat_chunk)
        self._chat_done_sig.connect(self._on_chat_done)
        self._chat_progress_sig.connect(self._on_chat_progress)
        self._input_level_sig.connect(self._set_input_audio_level)
        self._mic_overlay_sig.connect(self._set_mic_overlay_visible)
        self._mic_overlay = None   # created on first OPEN_MIC
        self.setWindowTitle(self._title())
        self.setWindowIcon(self._app_icon())
        self._quitting = False
        self._build_tray()

        self._overlay: SetupOverlay | None = None
        self._ready = self._check_config()
        if not self._ready:
            self._show_setup()

        QShortcut(QKeySequence("F4"),  self).activated.connect(self._toggle_mute)
        QShortcut(QKeySequence("F11"), self).activated.connect(self._toggle_fullscreen)
        QShortcut(QKeySequence("Ctrl+Space"), self).activated.connect(self._show_from_anywhere)
        QShortcut(QKeySequence("Alt+J"), self).activated.connect(self._show_from_anywhere)
        QShortcut(QKeySequence("Escape"), self).activated.connect(self.hide)
        QShortcut(QKeySequence("F12"), self).activated.connect(self._toggle_friday_dev)
        
        # JarvisApp integration
        self._jarvis_app = None

    def set_jarvis_app(self, app):
        """Connect to JarvisApp for state management."""
        self._jarvis_app = app

    # ── desktop-app lifecycle: tray, hide-to-tray, show-from-anywhere ───

    def _app_icon(self) -> QIcon:
        ico = BASE_DIR / "assets" / "jarvis.ico"
        if ico.exists():
            return QIcon(str(ico))
        # painted fallback: arc-reactor orb, needs no asset file
        pm = QPixmap(64, 64); pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm); p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setBrush(QColor("#04141c")); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(2, 2, 60, 60)
        p.setPen(QPen(QColor(C.PRI), 5)); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawEllipse(10, 10, 44, 44)
        p.setBrush(QColor(C.PRI)); p.setPen(Qt.PenStyle.NoPen)
        p.drawEllipse(26, 26, 12, 12)
        p.end()
        return QIcon(pm)

    def _build_tray(self):
        self._tray = QSystemTrayIcon(self._app_icon(), self)
        self._tray.setToolTip("JARVIS — say 'Jarvis' to wake")
        menu = QMenu()
        menu.addAction("Open JARVIS", self._show_from_anywhere)
        self._tray_pause = menu.addAction("Pause Listening", self._tray_toggle_listen)
        menu.addSeparator()
        menu.addAction("Restart", self._restart_app)
        menu.addAction("Exit", self._quit_app)
        self._tray.setContextMenu(menu)
        self._tray.activated.connect(
            lambda r: self._show_from_anywhere()
            if r == QSystemTrayIcon.ActivationReason.Trigger else None)
        self._tray.show()

    def _show_from_anywhere(self):
        """Instantly show and activate the window with smooth animation.
        
        This is called when wake word is detected — must be immediate.
        """
        try:
            print(f"[Desktop] Showing window instantly (visible={self.isVisible()}, "
                  f"minimized={self.isMinimized()})")
            
            # If minimized, restore it
            if self.isMinimized():
                self.showNormal()
            
            # If hidden, show it
            if not self.isVisible():
                self.show()
            
            # Force to front (Windows-specific trick)
            self.setWindowState(
                (self.windowState() & ~Qt.WindowState.WindowMinimized)
                | Qt.WindowState.WindowActive)
            
            # Quick topmost pulse to force foreground
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
            self.show()
            self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
            self.show()
            
            # Final activation
            self.raise_()
            self.activateWindow()
            
            print(f"[Desktop] Window activated instantly (visible={self.isVisible()})")
            
        except Exception as e:
            print(f"[Desktop] Window activation FAILED: {e}")
            import traceback
            traceback.print_exc()

    def _tray_toggle_listen(self):
        self._toggle_mute()
        self._tray_pause.setText(
            "Resume Listening" if self._muted else "Pause Listening")

    def _restart_app(self):
        import subprocess
        self._quitting = True
        if getattr(sys, "frozen", False):
            subprocess.Popen([sys.executable])
        else:
            subprocess.Popen([sys.executable, str(BASE_DIR / "main.py")])
        os._exit(0)

    def _quit_app(self):
        """Graceful shutdown: cleanup resources before exiting."""
        self._quitting = True
        self._tray.hide()
        print("[Shutdown] Starting graceful shutdown...")
        
        # Stop all timers
        for attr in ('_clock_tmr', '_metric_tmr', '_ease_tmr', '_ai_tmr'):
            tmr = getattr(self, attr, None)
            if tmr:
                tmr.stop()
        
        # Emit quit signal to jarvis thread
        try:
            self._log_sig.emit("SYS: Shutting down...")
        except Exception:
            pass
        
        # Give a brief moment for cleanup, then force exit
        import threading
        def _force_exit():
            import time
            time.sleep(1.5)
            print("[Shutdown] Force exit.")
            os._exit(0)
        
        # Start cleanup in background
        def _cleanup():
            try:
                # Close the NEXUS remote-control socket before anything else,
                # so no new remote request arrives mid-shutdown.
                from remote_control.service import shutdown_service as stop_bridge
                if stop_bridge().get("bridge_stopped"):
                    print("[Shutdown] NEXUS bridge closed.")
            except Exception:
                pass

            try:
                # Stop web-intelligence child processes (yt-dlp, feed parsers)
                from services.web_intelligence.tool import shutdown_service
                report = shutdown_service()
                if report.get("child_processes_stopped"):
                    print(f"[Shutdown] Web intelligence: stopped "
                          f"{report['child_processes_stopped']} child process(es).")
            except Exception:
                pass

            try:
                # Close any open Playwright browsers
                from actions.browser_control import _browser_contexts
                for ctx in list(_browser_contexts.values()):
                    try:
                        import asyncio
                        if asyncio.get_event_loop().is_running():
                            asyncio.get_event_loop().call_soon_threadsafe(ctx.close)
                    except Exception:
                        pass
            except Exception:
                pass
            
            try:
                # Stop sounddevice streams
                import sounddevice as sd
                sd.stop()
                sd.terminate()
            except Exception:
                pass
            
            print("[Shutdown] Cleanup done.")
        
        threading.Thread(target=_cleanup, daemon=True).start()
        threading.Thread(target=_force_exit, daemon=True).start()

    def closeEvent(self, ev):
        """X button: hide to tray. Shift+X or tray Exit: full shutdown."""
        if not self._quitting and self._tray.isVisible():
            ev.ignore()
            self.hide()
            self._tray.showMessage(
                "JARVIS is still running",
                "Say 'Jarvis' or click the tray icon to open.\n"
                "Right-click tray → Exit to quit completely.",
                QSystemTrayIcon.MessageIcon.Information, 3000)
        else:
            ev.accept()

    def _build_all(self):
        central = QWidget()
        central.setStyleSheet(f"background: {C.BG};")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0); root.setSpacing(0)
        self._header = self._build_header()
        root.addWidget(self._header)

        body = QHBoxLayout(); body.setContentsMargins(0, 0, 0, 0); body.setSpacing(0)
        self._nav = self._build_nav()
        body.addWidget(self._nav, stretch=0)

        self._pages = QStackedWidget()
        self._pages.addWidget(self._build_reference_home_page())       # 0
        self._pages.addWidget(self._build_chat_page())                 # 1
        self._pages.addWidget(self._build_files_page())                # 2
        self._pages.addWidget(self._build_calendar_page())             # 3
        self._pages.addWidget(self._build_settings_page())             # 4
        self._pages.addWidget(self._build_ai_page())                   # 5
        self._pages.addWidget(self._build_image_page())                # 6
        body.addWidget(self._pages, stretch=1)
        root.addLayout(body, stretch=1)
        self._dock = self._build_dock()
        root.addWidget(self._dock)

        self._nav_btns["HOME"].setChecked(True)
        if 0 in self._top_nav_btns:
            self._top_nav_btns[0].setChecked(True)
        if self._persona == "friday":
            self._nav.hide(); self._dock.hide()
            self._set_legacy_chrome_visible(False)
        self.hud.muted = self._muted
        self.hud.combat = self._theme == "battle"
        self._refresh_suggestions()
        self._refresh_events()
        self._refresh_project()

    def _title(self) -> str:
        if self._persona == "friday":
            # One FRIDAY, one title. Intensity is shown inside the interface,
            # never by presenting her as a different mode-specific product.
            return "FRIDAY — Intelligence System"
        if self._theme == "battle":
            return "J.A.R.V.I.S — ⚠ BATTLE MODE"
        if self._persona == "friday":
            return "F.R.I.D.A.Y — AI SYSTEM X"
        return "J.A.R.V.I.S — MARK X"

    def _rebuild_preserving(self):
        """Rebuild the whole UI, carrying over log, page, and loaded file."""
        log_html = self._log.toHtml() if hasattr(self, "_log") else ""
        pending: list[str] = []
        if hasattr(self, "_log"):
            # typewriter animation may still hold undrained lines — carry them over
            if self._log._typing and self._log._text:
                pending.append(self._log._text)
            pending.extend(self._log._queue)
        page     = self._pages.currentIndex() if hasattr(self, "_pages") else 0
        file_cur = self._drop_zone.current_file() if hasattr(self, "_drop_zone") else None
        self._build_all()
        self._log.setHtml(log_html)
        self._log.moveCursor(self._log.textCursor().MoveOperation.End)
        for line in pending:
            self._log.append_log(line)
        self._pages.setCurrentIndex(page)
        self._nav.setVisible(self._persona != "friday" or page != 0)
        self._dock.setVisible(self._persona != "friday" or page != 0)
        self._set_legacy_chrome_visible(self._persona != "friday" or page != 0)
        for n, b in self._nav_btns.items():
            b.setChecked(page == 0 and n == "HOME")
        for index, b in self._top_nav_btns.items():
            b.setChecked(index == page)
        if file_cur:
            self._drop_zone._set_file(file_cur)
        self._style_header_state()
        self.setWindowTitle(self._title())

    def set_theme(self, name: str):
        """Rebuild the whole UI in the given palette ('normal' | 'battle')."""
        if name not in THEMES or name == self._theme:
            return
        if self._persona == "friday" and not self._friday_mode_manager.set_mode(name, explicit=True):
            return
        self._theme = name
        _apply_palette(name)
        self._rebuild_preserving()
        if name == "battle":
            self._log.append_log("SYS: ⚠ BATTLE MODE ENGAGED. All systems combat-ready.")
        else:
            self._log.append_log("SYS: Battle mode disengaged. Returning to normal operations.")

    def set_persona(self, name: str):
        """Switch UI identity: 'friday' (animated face) | 'jarvis' (orb)."""
        name = str(name).lower()
        if name not in ("friday", "jarvis") or name == self._persona:
            return
        self._persona = name
        self._rebuild_preserving()
        self._log.append_log("SYS: FRIDAY persona active. Hello!" if name == "friday"
                             else "SYS: JARVIS persona restored.")

    def _set_emotion(self, name: str):
        if hasattr(self.hud, "set_emotion"):
            self.hud.set_emotion(name)
        if getattr(self, "_deck", None) is not None:
            self._deck.face.set_emotion(name)


    # ── header ───────────────────────────────────────────────────────────
    def _build_header(self) -> QWidget:
        w = QWidget(); w.setFixedHeight(66)
        w.setStyleSheet(f"background: {C.DARK}; border-bottom: 1px solid {C.BORDER_B};")
        lay = QHBoxLayout(w); lay.setContentsMargins(16, 0, 16, 0)

        logo_txt, mark_txt = (("◉  FRIDAY", "AI SYSTEM X") if self._persona == "friday"
                              else ("◉  JARVIS", "MARK X"))
        logo = QLabel(logo_txt); logo.setFont(QFont("Courier New", 16, QFont.Weight.Bold))
        logo.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        mark = QLabel(mark_txt); mark.setFont(QFont("Courier New", 7))
        mark.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lcol = QVBoxLayout(); lcol.setSpacing(0)
        lcol.addWidget(logo); lcol.addWidget(mark)
        lay.addLayout(lcol)
        deck = QLabel("COMMAND DECK" if self._theme == "normal" else "TACTICAL DECK")
        deck.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        deck.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; padding-left: 18px;")
        lay.addWidget(deck)

        self._top_nav_btns: dict[int, QPushButton] = {}
        if self._persona == "friday":
            for label, idx, name in [
                ("DASHBOARD", 0, "HOME"), ("CHAT", 1, "AI CHAT"),
                ("WORKSPACE", 2, "FILES"), ("AI STUDIO", 6, "IMAGE STUDIO"),
                ("AUTOMATION", 5, "AI CORE"), ("SYSTEM", 4, "SETTINGS"),
            ]:
                b = QPushButton(label)
                b.setCheckable(True)
                b.setFixedHeight(30)
                b.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
                b.setCursor(Qt.CursorShape.PointingHandCursor)
                b.setStyleSheet(f"""
                    QPushButton {{ background: transparent; color: {C.TEXT_DIM}; border: none;
                        border-bottom: 2px solid transparent; padding: 0 6px; }}
                    QPushButton:hover {{ color: {C.TEXT}; }}
                    QPushButton:checked {{ color: {C.PRI}; border-bottom-color: {C.PRI}; }}
                """)
                b.clicked.connect(lambda _, i=idx, n=name: self._switch_page(i, n))
                self._top_nav_btns[idx] = b
                lay.addWidget(b)
        lay.addStretch()

        mid = QVBoxLayout(); mid.setSpacing(0)
        self._clock_lbl = QLabel("00:00:00")
        self._clock_lbl.setFont(QFont("Courier New", 17, QFont.Weight.Bold))
        self._clock_lbl.setStyleSheet(f"color: {C.WHITE}; background: transparent;")
        self._clock_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._date_lbl = QLabel("")
        self._date_lbl.setFont(QFont("Courier New", 8))
        self._date_lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        self._date_lbl.setAlignment(Qt.AlignmentFlag.AlignCenter)
        mid.addWidget(self._clock_lbl); mid.addWidget(self._date_lbl)
        lay.addLayout(mid); lay.addStretch()

        # always-visible AI status chip — click opens the AI CORE page
        self._ai_chip = QPushButton("◈ AI  AUTO")
        self._ai_chip.setFixedHeight(26)
        self._ai_chip.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._ai_chip.setCursor(Qt.CursorShape.PointingHandCursor)
        self._ai_chip.setToolTip("AI Control Center")
        self._ai_chip.clicked.connect(lambda: self._switch_page(5, "AI CORE"))
        self._style_ai_chip(manual=False, battle=False)
        lay.addWidget(self._ai_chip)

        if self._persona == "friday":
            self._mode_btn = QPushButton()
            self._mode_btn.setFixedHeight(26)
            self._mode_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            self._mode_btn.setCursor(Qt.CursorShape.PointingHandCursor)
            self._mode_btn.setToolTip("Switch FRIDAY Normal / Battle mode")
            self._mode_btn.clicked.connect(self._toggle_theme)
            self._style_mode_switch()
            lay.addWidget(self._mode_btn)

        self._mic_btn = QPushButton("🎙")
        self._mic_btn.setFixedSize(34, 34)
        self._mic_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._mic_btn.setToolTip("Sleep / wake FRIDAY  [F4]" if self._persona == "friday" else "Sleep / wake JARVIS  [F4]")
        self._mic_btn.clicked.connect(self._toggle_mute)
        lay.addWidget(self._mic_btn)

        self._online_lbl = QPushButton("ONLINE")
        self._online_lbl.setFixedHeight(26)
        self._online_lbl.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        self._online_lbl.setEnabled(False)
        lay.addWidget(self._online_lbl)
        self._style_header_state()
        return w

    def _toggle_theme(self):
        self.set_theme("battle" if self._theme == "normal" else "normal")

    def _toggle_friday_dev(self):
        if self._persona == "friday" and getattr(self, "_deck", None) is not None:
            self._deck._toggle_dev()

    def _style_mode_switch(self):
        if not hasattr(self, "_mode_btn"):
            return
        active = self._theme == "battle"
        fg = C.PRI if active else C.TEXT_MED
        bg = C.PRI_GHO if active else C.PANEL2
        self._mode_btn.setText("⚔ BATTLE" if active else "◇ NORMAL")
        self._mode_btn.setStyleSheet(
            f"QPushButton {{ background: {bg}; color: {fg}; border: 1px solid {C.PRI_DIM if active else C.BORDER}; "
            "border-radius: 4px; padding: 0 9px; }}"
            f"QPushButton:hover {{ border-color: {C.PRI}; color: {C.PRI}; }}")

    def _style_header_state(self):
        if self._muted:
            self._mic_btn.setStyleSheet(
                f"QPushButton {{ background: #140006; color: {C.MUTED_C};"
                f"border: 1px solid {C.MUTED_C}; border-radius: 17px; }}")
            self._online_lbl.setText("ASLEEP")
            self._online_lbl.setStyleSheet(
                f"QPushButton {{ background: #140006; color: {C.MUTED_C};"
                f"border: 1px solid {C.MUTED_C}; border-radius: 4px; padding: 0 10px; }}")
        else:
            self._mic_btn.setStyleSheet(
                f"QPushButton {{ background: #00140a; color: {C.GREEN};"
                f"border: 1px solid {C.GREEN}; border-radius: 17px; }}")
            self._online_lbl.setText("ONLINE")
            self._online_lbl.setStyleSheet(
                f"QPushButton {{ background: #00140a; color: {C.GREEN};"
                f"border: 1px solid {C.GREEN}; border-radius: 4px; padding: 0 10px; }}")

    # ── left nav ─────────────────────────────────────────────────────────
    def _build_nav(self) -> QWidget:
        w = QWidget(); w.setFixedWidth(164)
        w.setStyleSheet(f"background: {C.DARK}; border-right: 1px solid {C.BORDER};")
        lay = QVBoxLayout(w); lay.setContentsMargins(0, 10, 0, 8); lay.setSpacing(1)
        sector = QLabel("NAVIGATION  //  " + ("TACTICAL" if self._theme == "battle" else "WORKSPACE"))
        sector.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        sector.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; padding: 0 12px 7px;")
        lay.addWidget(sector)

        self._nav_btns: dict[str, NavButton] = {}
        pages = [("🏠", "HOME", 0), ("💬", "AI CHAT", 1), ("📁", "FILES", 2),
                 ("📅", "CALENDAR", 3), ("⚙", "SETTINGS", 4), ("🧠", "AI CORE", 5),
                 ("🎨", "IMAGE STUDIO", 6)]
        for icon, name, idx in pages:
            b = NavButton(icon, name)
            b.clicked.connect(lambda _, i=idx, n=name: self._switch_page(i, n))
            lay.addWidget(b); self._nav_btns[name] = b

        sep = QFrame(); sep.setFrameShape(QFrame.Shape.HLine)
        sep.setStyleSheet(f"color: {C.BORDER}; margin: 6px 10px;")
        lay.addWidget(sep)

        # command shortcuts — route straight to Jarvis
        cmds = [("🌐", "BROWSER",    "open my browser"),
                ("⚡", "AUTOMATION", "what repetitive task can you automate for me?"),
                ("📝", "NOTES",      "open notepad"),
                ("☀",  "WEATHER",    "what's the weather like?"),
                ("🎵", "MUSIC",      "open spotify"),
                ("🖥", "TERMINAL",   "open the terminal")]
        for icon, name, cmd in cmds:
            b = NavButton(icon, name)
            b.setCheckable(False)
            b.clicked.connect(lambda _, c=cmd: self._send_cmd(c))
            lay.addWidget(b)

        lay.addStretch()
        self._nav_status = QLabel("◉ FRIDAY ONLINE" if self._persona == "friday" else "◉ JARVIS ONLINE")
        self._nav_status.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._nav_status.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._nav_status.setStyleSheet(f"color: {C.GREEN}; background: transparent; padding: 8px;")
        lay.addWidget(self._nav_status)
        return w

    def _switch_page(self, idx: int, name: str):
        self._pages.setCurrentIndex(idx)
        self._nav.setVisible(self._persona != "friday" or idx != 0)
        self._dock.setVisible(self._persona != "friday" or idx != 0)
        self._set_legacy_chrome_visible(self._persona != "friday" or idx != 0)
        for n, b in self._nav_btns.items():
            b.setChecked(n == name)
        for page_idx, b in self._top_nav_btns.items():
            b.setChecked(page_idx == idx)

    # ── HOME page ────────────────────────────────────────────────────────
    def _panel(self, title: str) -> tuple[QWidget, QVBoxLayout]:
        if self._persona == "friday":
            w = OpsPanel(title)
            margins = (12, 11, 12, 10)
        else:
            w = QWidget()
            w.setStyleSheet(f"background: {C.PANEL}; border: 1px solid {C.BORDER}; border-radius: 8px;")
            margins = (10, 8, 10, 8)
        lay = QVBoxLayout(w); lay.setContentsMargins(*margins); lay.setSpacing(5)
        t = QLabel(title); t.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        lay.addWidget(t)
        return w, lay

    def _build_reference_home_page(self) -> QWidget:
        """Use FRIDAY's dashboard while retaining legacy plumbing off-screen."""
        if self._persona != "friday":
            self._friday_deck_active = False
            self._deck = None
            self._friday_ui = None
            return self._build_home_page(self._face_path)
        page = QWidget()
        lay = QVBoxLayout(page); lay.setContentsMargins(0, 0, 0, 0)
        # Set before building the legacy page so it knows not to construct the
        # procedural face for FRIDAY.
        self._friday_deck_active = True
        self._legacy_home = self._build_home_page(self._face_path)
        self._legacy_home.setParent(page)
        self._legacy_home.hide()
        self._deck = None

        # FRIDAY's face and dashboard live in the friday package; the video
        # avatar replaces the old procedurally drawn face entirely.
        from friday.bridge import FridayBridge, connect as connect_bridge
        from friday.window import FridayInterface

        self._friday_ui = FridayInterface(page)
        self._friday_bridge = FridayBridge(self)
        connect_bridge(self._friday_bridge, self._friday_ui)

        self._friday_ui.stage.submitted.connect(self._send_cmd)
        self._friday_ui.stage.mic_clicked.connect(self._toggle_mute)
        self._friday_ui.stage.attach_clicked.connect(
            lambda: self._switch_page(2, "FILES"))
        self._friday_ui.dock.activated.connect(self._on_deck_action)
        for key, reason in self._FRIDAY_DOCK_UNBUILT.items():
            self._friday_ui.dock.set_available(key, False, reason)
        self._friday_ui.voice_panel.mute_toggled.connect(self._set_muted)
        self._friday_bridge.on_mic_muted(self._muted)

        lay.addWidget(self._friday_ui)
        return page

    def _set_legacy_chrome_visible(self, visible: bool):
        """Hide the legacy header on FRIDAY's home page.

        FRIDAY's interface carries its own top bar; showing both stacks two
        clocks and two identities on top of each other. JARVIS and every other
        page keep the original header untouched.
        """
        header = getattr(self, "_header", None)
        if header is not None:
            header.setVisible(visible)

    def _on_deck_action(self, action: str):
        pages = {
            "chat": (1, "AI CHAT"), "files": (2, "FILES"),
            "documents": (2, "FILES"),
            "settings": (4, "SETTINGS"), "image": (6, "IMAGE STUDIO"),
            "images": (6, "IMAGE STUDIO"),
        }
        if action in pages:
            self._switch_page(*pages[action])
        elif action == "mic":
            self._toggle_mute()
        elif action == "automation":
            self._send_cmd("what repetitive task can you automate for me?")
        elif action == "research":
            self._send_cmd("research the latest information on ")
        elif action == "music":
            self._send_cmd("open spotify")
        else:
            # Reached only if a tile is enabled without a destination. Say so
            # rather than appear to work and do nothing.
            self._log.append_log(f"SYS: '{action}' is not available yet.")

    #: Dock tiles with no destination yet. They render disabled and explain
    #: themselves on hover instead of silently doing nothing when clicked.
    _FRIDAY_DOCK_UNBUILT = {
        "video": "Video Studio is not built yet.",
        "voice": "Voice Studio is not built yet — use the microphone control.",
        "code": "Code Studio is not built yet — ask FRIDAY in chat instead.",
    }

    def _build_home_page(self, face_path: str) -> QWidget:
        page = QWidget()
        grid = QHBoxLayout(page)
        grid.setContentsMargins(10, 10, 10, 10); grid.setSpacing(10)

        # column 1 — overview / quick actions / project
        col1 = QVBoxLayout(); col1.setSpacing(10)

        ov, ov_lay = self._panel("SYSTEM TELEMETRY" if self._theme == "battle" else "SYSTEM OVERVIEW")
        gauges = QGridLayout(); gauges.setSpacing(4)
        self._g_cpu  = RingGauge("CPU",  C.PRI)
        self._g_ram  = RingGauge("RAM",  C.GREEN)
        self._g_gpu  = RingGauge("GPU",  "#44ddaa")
        self._g_disk = RingGauge("DISK", C.ACC2)
        gauges.addWidget(self._g_cpu, 0, 0);  gauges.addWidget(self._g_ram, 0, 1)
        gauges.addWidget(self._g_gpu, 1, 0);  gauges.addWidget(self._g_disk, 1, 1)
        ov_lay.addLayout(gauges)
        col1.addWidget(ov, stretch=3)

        qa, qa_lay = self._panel("TACTICAL ACTIONS" if self._theme == "battle" else "QUICK ACTIONS")
        qgrid = QGridLayout(); qgrid.setSpacing(5)
        actions = [
            ("🧑‍💻 VS Code",     lambda: _launch("code" if _OS != "Windows" else "code", self._log_sig.emit)),
            ("🌐 Chrome",       lambda: _launch("chrome"if _OS == "Windows" else "google-chrome", self._log_sig.emit)),
            ("📸 Screenshot",   self._qa_screenshot),
            ("⏺ Record",        self._qa_record),
            ("🧠 AI Summary",   lambda: self._send_cmd("give me a quick summary of my system status and today")),
            ("🚀 Optimize",     lambda: self._send_cmd("optimize my system performance")),
            ("🧹 Clear Temp",   self._qa_clear_temp),
            ("😴 Sleep",        lambda: self._set_muted(True)),
        ]
        self._qa_btns = {}
        for i, (txt, cb) in enumerate(actions):
            b = QPushButton(txt)
            b.setFixedHeight(30)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{ background: {C.PANEL2}; color: {C.TEXT};
                    border: 1px solid {C.BORDER_A}; border-radius: 5px; }}
                QPushButton:hover {{ border: 1px solid {C.PRI}; color: {C.PRI};
                    background: {C.PRI_GHO}; }}
            """)
            b.clicked.connect(cb)
            qgrid.addWidget(b, i // 2, i % 2)
            self._qa_btns[txt] = b
        qa_lay.addLayout(qgrid)
        col1.addWidget(qa, stretch=0)

        pr, pr_lay = self._panel("ACTIVE PROJECT")
        self._proj_lbl = QLabel("No active projects yet")
        self._proj_lbl.setFont(QFont("Courier New", 8))
        self._proj_lbl.setWordWrap(True)
        self._proj_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
        pr_lay.addWidget(self._proj_lbl)
        pr_lay.addStretch()
        col1.addWidget(pr, stretch=2)

        c1 = QWidget(); c1.setLayout(col1)
        c1.setMinimumWidth(240); c1.setMaximumWidth(320)
        grid.addWidget(c1, stretch=2)

        # column 2 — wave / core / input / convo+suggestions
        col2 = QVBoxLayout(); col2.setSpacing(8)
        self._wave = WaveStrip()
        col2.addWidget(self._wave)

        mode_line = QWidget()
        mode_line.setFixedHeight(22)
        mode_line.setStyleSheet(f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px;")
        mode_lay = QHBoxLayout(mode_line); mode_lay.setContentsMargins(9, 0, 9, 0)
        self._mode_context = QLabel()
        self._mode_context.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        self._mode_context.setStyleSheet(f"color: {C.PRI}; background: transparent; border: none;")
        self._mode_context.setText(
            "⚔ DEFENSE GRID ACTIVE  //  PRIORITY: PROTECT & RESPOND"
            if self._theme == "battle" else
            "◇ AI OPERATING SYSTEM  //  OMNIROUTE CONNECTED  //  ALL SYSTEMS NOMINAL")
        mode_lay.addWidget(self._mode_context); mode_lay.addStretch()
        mode_dot = QLabel("● LIVE")
        mode_dot.setFont(QFont("Courier New", 7, QFont.Weight.Bold))
        mode_dot.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        mode_lay.addWidget(mode_dot)
        col2.addWidget(mode_line)

        # FRIDAY's face is the video avatar in FridayInterface. The legacy
        # procedural FaceCanvas is deliberately not built for her — this widget
        # only survives as the off-screen `self.hud` the older plumbing pokes at.
        hud_cls = FaceCanvas if (
            self._persona == "friday" and self._theme != "battle"
            and not getattr(self, "_friday_deck_active", False)) else HudCanvas
        self.hud = hud_cls(face_path)
        self.hud.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        col2.addWidget(self.hud, stretch=5)

        ask = QLabel(
            "TACTICAL DIRECTIVE — state your objective" if self._theme == "battle" else
            ("How can I assist you today?" if self._persona == "friday"
             else "How can I help you today?"))
        ask.setFont(QFont("Courier New", 12, QFont.Weight.Bold))
        ask.setAlignment(Qt.AlignmentFlag.AlignCenter)
        ask.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        col2.addWidget(ask)

        row = QHBoxLayout(); row.setSpacing(6)
        self._input = QLineEdit()
        self._input.setPlaceholderText("Type a command or ask anything…")
        self._input.setFont(QFont("Courier New", 10))
        self._input.setFixedHeight(38)
        self._input.setStyleSheet(f"""
            QLineEdit {{ background: {C.PANEL}; color: {C.WHITE};
                border: 1px solid {C.BORDER_B}; border-radius: 19px; padding: 4px 16px; }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._input.returnPressed.connect(lambda: self._send_from(self._input))
        row.addWidget(self._input)
        send = QPushButton("➤"); send.setFixedSize(38, 38)
        send.setCursor(Qt.CursorShape.PointingHandCursor)
        send.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 19px; font-size: 14px; }}
            QPushButton:hover {{ border: 1px solid {C.PRI}; }}
        """)
        send.clicked.connect(lambda: self._send_from(self._input))
        row.addWidget(send)
        col2.addLayout(row)

        bottom = QHBoxLayout(); bottom.setSpacing(10)
        rc, rc_lay = self._panel("RECENT CONVERSATIONS")
        self._convo_lbl = QLabel("Nothing yet — say 'Jarvis' or type below")
        self._convo_lbl.setFont(QFont("Courier New", 7))
        self._convo_lbl.setWordWrap(True)
        self._convo_lbl.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent; border: none;")
        rc_lay.addWidget(self._convo_lbl); rc_lay.addStretch()
        bottom.addWidget(rc, stretch=1)

        sg, sg_lay = self._panel("SUGGESTIONS FOR YOU")
        self._sug_lay = sg_lay
        bottom.addWidget(sg, stretch=1)
        col2.addLayout(bottom, stretch=2)

        c2 = QWidget(); c2.setLayout(col2)
        grid.addWidget(c2, stretch=1)

        # column 3 — vision / status / events / media
        col3 = QVBoxLayout(); col3.setSpacing(10)

        vi, vi_lay = self._panel("THREAT MATRIX" if self._theme == "battle" else "JARVIS VISION")
        self._globe = VisionGlobe()
        self._globe.clicked.connect(lambda: self._send_cmd("what is on my screen?"))
        vi_lay.addWidget(self._globe)
        vm = QLabel("● PERIMETER SCAN: click matrix to analyze" if self._theme == "battle"
                    else "● VISION MODE: click globe to scan")
        vm.setFont(QFont("Courier New", 7))
        vm.setStyleSheet(f"color: {C.GREEN}; background: transparent; border: none;")
        vi_lay.addWidget(vm)
        col3.addWidget(vi, stretch=3)

        st, st_lay = self._panel("COMBAT READINESS" if self._theme == "battle" else "SYSTEM STATUS")
        self._bar_bat = MetricBar("BAT", "#ffcc44")
        self._bar_tmp = MetricBar("TMP", "#ff6688")
        self._bar_net = MetricBar("NET", C.GREEN)
        self._bar_up  = MetricBar("UPTIME", "#bb88ff")
        for b in (self._bar_bat, self._bar_tmp, self._bar_net, self._bar_up):
            st_lay.addWidget(b)
        col3.addWidget(st, stretch=0)

        ev, ev_lay = self._panel("UPCOMING EVENTS")
        self._events_lbl = QLabel("No events — add them in CALENDAR")
        self._events_lbl.setFont(QFont("Courier New", 7))
        self._events_lbl.setWordWrap(True)
        self._events_lbl.setStyleSheet(f"color: {C.TEXT}; background: transparent; border: none;")
        ev_lay.addWidget(self._events_lbl); ev_lay.addStretch()
        col3.addWidget(ev, stretch=2)

        md, md_lay = self._panel("MEDIA CONTROL")
        mrow = QHBoxLayout(); mrow.setSpacing(8)
        for sym, key in [("⏮", "prevtrack"), ("⏯", "playpause"), ("⏭", "nexttrack")]:
            b = QPushButton(sym); b.setFixedSize(44, 34)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{ background: {C.PANEL2}; color: {C.PRI}; font-size: 14px;
                    border: 1px solid {C.BORDER_A}; border-radius: 6px; }}
                QPushButton:hover {{ border: 1px solid {C.PRI}; background: {C.PRI_GHO}; }}
            """)
            b.clicked.connect(lambda _, k=key: self._media_key(k))
            mrow.addWidget(b)
        mrow.addStretch()
        md_lay.addLayout(mrow)
        col3.addWidget(md, stretch=0)

        c3 = QWidget(); c3.setLayout(col3)
        c3.setMinimumWidth(230); c3.setMaximumWidth(300)
        grid.addWidget(c3, stretch=2)
        return page

    # ── other pages ──────────────────────────────────────────────────────
    def _build_chat_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page); lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(8)
        t = QLabel("💬  AI CHAT — full conversation log")
        t.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(t)
        toolbar = QHBoxLayout(); toolbar.setSpacing(5)
        new_btn = QPushButton("NEW CHAT")
        new_btn.clicked.connect(self._chat_new)
        self._chat_picker = QComboBox()
        self._chat_picker.currentIndexChanged.connect(self._chat_pick_changed)
        self._chat_picker.setMinimumWidth(220)
        self._chat_search = QLineEdit()
        self._chat_search.setPlaceholderText("Search chats…")
        self._chat_search.textChanged.connect(self._chat_refresh_list)
        self._chat_pin_btn = QPushButton("PIN")
        self._chat_pin_btn.clicked.connect(self._chat_toggle_pin)
        folder_btn = QPushButton("FOLDER")
        folder_btn.clicked.connect(self._chat_set_folder)
        export_btn = QPushButton("EXPORT")
        export_btn.clicked.connect(self._chat_export)
        for button in (new_btn, self._chat_pin_btn, folder_btn, export_btn):
            button.setStyleSheet(f"QPushButton {{ color: {C.TEXT_MED}; background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 4px; padding: 5px 8px; }} QPushButton:hover {{ color: {C.PRI}; border-color: {C.PRI}; }}")
        toolbar.addWidget(new_btn); toolbar.addWidget(self._chat_picker, stretch=1)
        toolbar.addWidget(self._chat_search); toolbar.addWidget(self._chat_pin_btn)
        toolbar.addWidget(folder_btn); toolbar.addWidget(export_btn)
        lay.addLayout(toolbar)
        self._chat_transcript = QTextEdit()
        self._chat_transcript.setReadOnly(True)
        self._chat_transcript.setStyleSheet(f"QTextEdit {{ background: {C.PANEL}; color: {C.TEXT}; border: 1px solid {C.BORDER}; border-radius: 6px; padding: 8px; }}")
        lay.addWidget(self._chat_transcript, stretch=1)
        self._log = LogWidget()  # retained for voice/system logging compatibility
        self._log.hide()
        row = QHBoxLayout()
        attach_btn = QPushButton("ATTACH")
        attach_btn.clicked.connect(self._chat_attach)
        row.addWidget(attach_btn)
        self._chat_input = QLineEdit()
        self._chat_input.setPlaceholderText("Message FRIDAY…" if self._persona == "friday" else "Message JARVIS…")
        self._chat_input.setFont(QFont("Courier New", 10))
        self._chat_input.setFixedHeight(36)
        self._chat_input.setStyleSheet(f"""
            QLineEdit {{ background: {C.PANEL}; color: {C.WHITE};
                border: 1px solid {C.BORDER_B}; border-radius: 18px; padding: 4px 14px; }}
            QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
        """)
        self._chat_input.returnPressed.connect(self._chat_submit)
        row.addWidget(self._chat_input)
        self._chat_send_btn = QPushButton("SEND")
        self._chat_send_btn.clicked.connect(self._chat_submit)
        row.addWidget(self._chat_send_btn)
        lay.addLayout(row)
        self._chat_status = QLabel("OmniRoute streaming · voice remains available through the microphone")
        self._chat_status.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(self._chat_status)
        self._chat_refresh_list()
        self._chat_render()
        return page

    def _chat_refresh_list(self, *_):
        query = self._chat_search.text() if hasattr(self, "_chat_search") else ""
        conversations = self._chat_studio.conversations(query)
        self._chat_picker.blockSignals(True)
        self._chat_picker.clear()
        selected_index = 0
        for index, conversation in enumerate(conversations):
            label = ("★ " if conversation["pinned"] else "") + conversation["title"]
            if conversation.get("folder"):
                label += f"  [{conversation['folder']}]"
            self._chat_picker.addItem(label, conversation["id"])
            if conversation["id"] == self._chat_studio.active_id:
                selected_index = index
        if conversations:
            self._chat_picker.setCurrentIndex(selected_index)
        self._chat_picker.blockSignals(False)

    def _chat_pick_changed(self, _index):
        conversation_id = self._chat_picker.currentData()
        if conversation_id and conversation_id != self._chat_studio.active_id:
            self._chat_studio.select(conversation_id)
            self._chat_partial = ""
            self._chat_render()

    def _chat_new(self):
        self._chat_studio.create_conversation()
        self._chat_partial = ""
        self._chat_refresh_list(); self._chat_render(); self._chat_input.setFocus()

    def _chat_toggle_pin(self):
        conversation = self._chat_studio.active()
        self._chat_studio.set_pinned(conversation["id"], not conversation["pinned"])
        self._chat_refresh_list(); self._chat_render()

    def _chat_set_folder(self):
        conversation = self._chat_studio.active()
        folder, accepted = QInputDialog.getText(self, "Chat folder", "Folder:", text=conversation.get("folder", ""))
        if accepted:
            self._chat_studio.set_folder(conversation["id"], folder)
            self._chat_refresh_list(); self._chat_render()

    def _chat_attach(self):
        paths, _ = QFileDialog.getOpenFileNames(self, "Attach to chat", str(Path.home()), "All Files (*.*)")
        if not paths:
            return
        try:
            self._chat_attachments = self._chat_studio.attach_paths(paths)
            self._chat_status.setText("Processing: " + ", ".join(item.path.name for item in self._chat_attachments))
            self._chat_start_request("Analyze the attached file(s) and prepare a concise structured summary for follow-up questions.")
        except Exception as exc:
            self._chat_status.setText(f"Attachment rejected: {exc}")

    def _chat_export(self):
        try:
            path = self._chat_studio.export_markdown(self._chat_studio.active_id)
            self._chat_status.setText(f"Exported: {path}")
        except Exception as exc:
            self._chat_status.setText(f"Export failed: {exc}")

    def _chat_render(self):
        conversation = self._chat_studio.active()
        self._chat_pin_btn.setText("UNPIN" if conversation["pinned"] else "PIN")
        pages = []
        for message in conversation["messages"]:
            label = "You" if message["role"] == "user" else ("FRIDAY" if self._persona == "friday" else "JARVIS")
            files = [f"- Attachment: `{Path(item['path']).name}` ({item['kind']})" for item in message.get("attachments", [])]
            pages.append(f"## {label}\n\n{message['content']}" + ("\n\n" + "\n".join(files) if files else ""))
        if self._chat_partial:
            pages.append(f"## {'FRIDAY' if self._persona == 'friday' else 'JARVIS'}\n\n{self._chat_partial}▌")
        self._chat_transcript.setMarkdown("\n\n---\n\n".join(pages) or "# New conversation\n\nHow can I help?")
        bar = self._chat_transcript.verticalScrollBar(); bar.setValue(bar.maximum())

    def _chat_submit(self):
        text = self._chat_input.text().strip()
        if not text or not self._chat_input.isEnabled():
            return
        self._chat_input.clear()
        self._chat_start_request(text)

    def _chat_start_request(self, text: str):
        if not text or not self._chat_input.isEnabled():
            return
        self._chat_partial = ""
        self._chat_input.setEnabled(False); self._chat_send_btn.setEnabled(False)
        self._chat_status.setText("Streaming response…")
        from Studios.contracts import StudioRequest
        request = StudioRequest(prompt=text, attachments=list(self._chat_attachments), conversation_id=self._chat_studio.active_id)
        self._chat_attachments = []
        def work():
            result = self._chat_studio.stream_response(request, self._chat_chunk_sig.emit,
                                                       self._chat_progress_sig.emit)
            self._chat_done_sig.emit((result.status == "completed", result))
        threading.Thread(target=work, daemon=True, name="ChatStudio").start()

    def _on_chat_chunk(self, chunk: str):
        self._chat_partial += chunk
        self._chat_render()

    def _on_chat_progress(self, context):
        if context.status == "failed":
            self._chat_status.setText(f"Could not process {context.name}: {context.error}")
        else:
            self._chat_status.setText(
                f"Ready: {context.name} · {context.mime_type} · routing to {context.route_task} model")

    def _on_chat_done(self, payload):
        ok, result = payload
        self._chat_partial = ""
        self._chat_input.setEnabled(True); self._chat_send_btn.setEnabled(True)
        self._chat_status.setText("Ready" if ok else f"Chat failed: {result.message[:140]}")
        self._chat_render(); self._chat_refresh_list(); self._chat_input.setFocus()

    def _build_files_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page); lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(8)
        t = QLabel("📁  FILES — drop anything, then tell FRIDAY what to do" if self._persona == "friday" else "📁  FILES — drop anything, then tell JARVIS what to do")
        t.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(t)
        self._drop_zone = FileDropZone()
        self._drop_zone.setFixedHeight(160)
        self._drop_zone.file_selected.connect(self._on_file_selected)
        lay.addWidget(self._drop_zone)
        self._file_hint = QLabel("No file loaded")
        self._file_hint.setFont(QFont("Courier New", 8))
        self._file_hint.setWordWrap(True)
        self._file_hint.setStyleSheet(f"color: {C.TEXT_MED}; background: transparent;")
        lay.addWidget(self._file_hint)
        for cmd, lbl in [("summarize this file", "📄 Summarize"),
                         ("extract the text from this file", "🔤 Extract text"),
                         ("analyze this file", "🔍 Analyze"),
                         ("explain this code file", "💻 Explain code")]:
            b = QPushButton(lbl); b.setFixedHeight(32)
            b.setFont(QFont("Courier New", 9))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{ background: {C.PANEL2}; color: {C.TEXT};
                    border: 1px solid {C.BORDER_A}; border-radius: 5px; }}
                QPushButton:hover {{ border: 1px solid {C.PRI}; color: {C.PRI}; }}
            """)
            b.clicked.connect(lambda _, c=cmd: self._send_cmd(c))
            lay.addWidget(b)
        lay.addStretch()
        return page

    def _build_calendar_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page); lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(8)
        t = QLabel("📅  CALENDAR — pick a date, add an event")
        t.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(t)
        self._cal = QCalendarWidget()
        self._cal.setStyleSheet(f"""
            QCalendarWidget QWidget {{ background: {C.PANEL}; color: {C.TEXT};
                alternate-background-color: {C.PANEL2}; }}
            QCalendarWidget QAbstractItemView:enabled {{
                background: {C.PANEL}; color: {C.TEXT};
                selection-background-color: {C.PRI_GHO}; selection-color: {C.PRI}; }}
            QCalendarWidget QToolButton {{ color: {C.PRI}; background: transparent; }}
        """)
        lay.addWidget(self._cal, stretch=1)
        row = QHBoxLayout()
        self._ev_input = QLineEdit()
        self._ev_input.setPlaceholderText("Event for the selected date… (Enter to save)")
        self._ev_input.setFixedHeight(32)
        self._ev_input.setFont(QFont("Courier New", 9))
        self._ev_input.setStyleSheet(f"""
            QLineEdit {{ background: {C.PANEL}; color: {C.WHITE};
                border: 1px solid {C.BORDER_B}; border-radius: 5px; padding: 3px 10px; }}
        """)
        self._ev_input.returnPressed.connect(self._add_event)
        row.addWidget(self._ev_input)
        lay.addLayout(row)
        self._ev_list = QLabel("")
        self._ev_list.setFont(QFont("Courier New", 8))
        self._ev_list.setWordWrap(True)
        self._ev_list.setStyleSheet(f"color: {C.TEXT}; background: transparent;")
        lay.addWidget(self._ev_list)
        return page

    def _build_settings_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page); lay.setContentsMargins(20, 16, 20, 16); lay.setSpacing(10)
        t = QLabel("⚙  SETTINGS")
        t.setFont(QFont("Courier New", 10, QFont.Weight.Bold))
        t.setStyleSheet(f"color: {C.PRI}; background: transparent;")
        lay.addWidget(t)

        def _field(label, placeholder):
            l = QLabel(label); l.setFont(QFont("Courier New", 8))
            l.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
            e = QLineEdit(); e.setPlaceholderText(placeholder)
            e.setFixedHeight(32); e.setFont(QFont("Courier New", 9))
            e.setStyleSheet(f"""
                QLineEdit {{ background: {C.PANEL}; color: {C.WHITE};
                    border: 1px solid {C.BORDER}; border-radius: 5px; padding: 3px 10px; }}
                QLineEdit:focus {{ border: 1px solid {C.PRI}; }}
            """)
            lay.addWidget(l); lay.addWidget(e)
            return e

        cfg = {}
        try:
            cfg = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        self._set_voice = _field("VOICE  (Charon · Puck · Kore · Fenrir · Aoede · Leda · Orus · Zephyr)",
                                 cfg.get("voice_name", "Charon"))
        self._set_voice.setText(cfg.get("voice_name", ""))
        self._set_model = _field("LIVE MODEL", cfg.get("live_model", ""))
        self._set_model.setText(cfg.get("live_model", ""))
        self._set_key = _field("GEMINI API KEY (leave blank to keep current)", "AIza…")
        self._set_key.setEchoMode(QLineEdit.EchoMode.Password)

        save = QPushButton("💾  SAVE — takes effect on next reconnect")
        save.setFixedHeight(36)
        save.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        save.setCursor(Qt.CursorShape.PointingHandCursor)
        save.setStyleSheet(f"""
            QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};
                border: 1px solid {C.PRI_DIM}; border-radius: 5px; }}
            QPushButton:hover {{ border: 1px solid {C.PRI}; }}
        """)
        save.clicked.connect(self._save_settings)
        lay.addWidget(save)
        lay.addStretch()
        return page

    def _save_settings(self):
        try:
            cfg = {}
            try:
                cfg = json.loads(API_FILE.read_text(encoding="utf-8"))
            except Exception:
                pass
            if self._set_voice.text().strip():
                cfg["voice_name"] = self._set_voice.text().strip()
            if self._set_model.text().strip():
                cfg["live_model"] = self._set_model.text().strip()
            if self._set_key.text().strip():
                cfg["gemini_api_key"] = self._set_key.text().strip()
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
            self._log_sig.emit("SYS: Settings saved — applies on next reconnect.")
        except Exception as e:
            self._log_sig.emit(f"ERR: settings — {e}")

    # ── AI CORE page — the AI Control Center ─────────────────────────────
    def _build_ai_page(self) -> QWidget:
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(10)

        def _lbl(text="", size=8, bold=False, color=C.TEXT):
            l = QLabel(text)
            l.setFont(QFont("Courier New", size,
                            QFont.Weight.Bold if bold else QFont.Weight.Normal))
            l.setStyleSheet(f"color: {color}; background: transparent; border: none;")
            l.setWordWrap(True)
            return l

        top = QHBoxLayout(); top.setSpacing(10)

        # ── ACTIVE MODEL card ──
        card, cl = self._panel("◈ ACTIVE MODEL")
        self._ai_model_lbl = _lbl("—", 15, True, C.WHITE)
        self._ai_provider_lbl = _lbl("provider: — · bucket: —", 8, False, C.TEXT_DIM)
        badge_row = QHBoxLayout(); badge_row.setSpacing(6)
        self._ai_mode_lbl = _lbl(" NORMAL MODE ", 8, True, C.PRI)
        self._ai_auto_lbl = _lbl(" AUTO ", 8, True, C.PRI)
        self._ai_health_dot = _lbl("● checking…", 8, True, C.TEXT_DIM)
        for b in (self._ai_mode_lbl, self._ai_auto_lbl, self._ai_health_dot):
            badge_row.addWidget(b)
        badge_row.addStretch()
        self._ai_meta_lbl = _lbl(
            "CONTEXT 1,000,000 tokens · VISION ✓ · TOOLS ✓ · REASONING ✓ · "
            "STREAMING SSE ✓", 7, False, C.TEXT_MED)
        self._ai_latency_lbl = _lbl("latency: — · last request: —", 8, False, C.TEXT_MED)
        cl.addWidget(self._ai_model_lbl); cl.addWidget(self._ai_provider_lbl)
        cl.addLayout(badge_row)
        cl.addWidget(self._ai_meta_lbl); cl.addWidget(self._ai_latency_lbl)
        top.addWidget(card, stretch=3)

        # ── ROUTING REASON card ──
        why, wl = self._panel("◈ ROUTING DECISION")
        self._ai_reason_lbl = _lbl("Waiting for the first AI request…", 9, False, C.TEXT)
        self._ai_conf_lbl = _lbl("confidence: —", 9, True, C.GREEN)
        self._ai_chain_lbl = _lbl("fallback: —", 7, False, C.TEXT_DIM)
        wl.addWidget(self._ai_reason_lbl); wl.addWidget(self._ai_conf_lbl)
        wl.addWidget(self._ai_chain_lbl); wl.addStretch()
        top.addWidget(why, stretch=2)
        lay.addLayout(top)

        # ── OVERRIDE buttons ──
        ovr, ol = self._panel("◈ MODEL OVERRIDE — routing is automatic unless pinned")
        row = QHBoxLayout(); row.setSpacing(6)
        self._ai_ovr_btns = {}
        for label, key in [("AUTO", None), ("CLAUDE", "claude"),
                           ("OPUS", "claude opus"), ("GEMINI", "gemini"),
                           ("GPT-5", "gpt-5"), ("DEEPSEEK", "deepseek"),
                           ("OLLAMA", "ollama")]:
            b = QPushButton(label)
            b.setFixedHeight(28)
            b.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.clicked.connect(lambda _, k=key: self._ai_set_override(k))
            row.addWidget(b); self._ai_ovr_btns[key] = b
        row.addStretch()
        ol.addLayout(row)

        # searchable model picker — all models from OmniRoute
        from PyQt6.QtWidgets import QComboBox, QLineEdit as _LE
        picker_row = QHBoxLayout(); picker_row.setSpacing(6)
        self._ai_model_search = _LE()
        self._ai_model_search.setPlaceholderText("Search / type model id…")
        self._ai_model_search.setFixedHeight(26)
        self._ai_model_search.setFont(QFont("Courier New", 8))
        self._ai_model_search.setStyleSheet(
            f"background: {C.PANEL2}; color: {C.WHITE}; border: 1px solid {C.BORDER};"
            "border-radius: 4px; padding: 0 8px;")
        self._ai_model_combo = QComboBox()
        self._ai_model_combo.setFixedHeight(26)
        self._ai_model_combo.setFont(QFont("Courier New", 8))
        self._ai_model_combo.setStyleSheet(
            f"QComboBox {{ background: {C.PANEL2}; color: {C.WHITE};"
            f"border: 1px solid {C.BORDER}; border-radius: 4px; padding: 0 8px; }}"
            f"QComboBox::drop-down {{ border: none; }}"
            f"QComboBox QAbstractItemView {{ background: {C.PANEL}; color: {C.WHITE};"
            f"selection-background-color: {C.PRI_GHO}; }}")
        self._ai_model_combo.addItem("Loading models…")
        self._ai_model_combo.setMinimumWidth(260)
        pin_btn = QPushButton("PIN")
        pin_btn.setFixedHeight(26)
        pin_btn.setFont(QFont("Courier New", 8, QFont.Weight.Bold))
        pin_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        pin_btn.setStyleSheet(
            f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};"
            f"border: 1px solid {C.PRI_DIM}; border-radius: 4px; padding: 0 12px; }}"
            f"QPushButton:hover {{ border: 1px solid {C.PRI}; }}")
        pin_btn.clicked.connect(self._ai_pin_selected)
        self._ai_model_search.textChanged.connect(self._ai_filter_models)
        picker_row.addWidget(self._ai_model_search, stretch=1)
        picker_row.addWidget(self._ai_model_combo, stretch=2)
        picker_row.addWidget(pin_btn)
        picker_row.addStretch()
        ol.addLayout(picker_row)
        lay.addWidget(ovr)
        self._ai_all_models: list[str] = []
        threading.Thread(target=self._ai_load_models, daemon=True,
                         name="AIModelList").start()

        # ── ROUTING HISTORY ──
        hist, hl = self._panel("◈ ROUTING HISTORY")
        self._ai_hist_lbl = _lbl("No AI requests yet this session.", 8, False, C.TEXT_MED)
        self._ai_hist_lbl.setTextFormat(Qt.TextFormat.RichText)
        hl.addWidget(self._ai_hist_lbl); hl.addStretch()
        lay.addWidget(hist, stretch=1)

        self._ai_last_served = None
        self._style_ai_override_btns(None)
        return page

    def _ai_load_models(self):
        """Background: fetch all OmniRoute models, populate combo."""
        try:
            from core.ai import base_url, _headers
            import requests as _req
            r = _req.get(f"{base_url()}/models", headers=_headers(), timeout=8)
            models = [m["id"] for m in r.json().get("data", [])]
            self._ai_all_models = sorted(models)
            # update combo on the UI thread
            QTimer.singleShot(0, lambda: self._ai_populate_combo(self._ai_all_models))
        except Exception as e:
            QTimer.singleShot(0, lambda: self._ai_model_combo.setItemText(
                0, f"Could not load models: {e}"))

    def _ai_populate_combo(self, models: list[str]):
        self._ai_model_combo.clear()
        for m in models:
            self._ai_model_combo.addItem(m)
        # show currently pinned model if any
        from core.ai import get_override
        ov = get_override()
        if ov and ov in models:
            self._ai_model_combo.setCurrentText(ov)

    def _ai_filter_models(self, text: str):
        filtered = [m for m in self._ai_all_models
                    if text.lower() in m.lower()] if text else self._ai_all_models
        self._ai_model_combo.blockSignals(True)
        self._ai_model_combo.clear()
        for m in filtered[:200]:   # ponytail: cap at 200 — combo with 483 items is sluggish
            self._ai_model_combo.addItem(m)
        self._ai_model_combo.blockSignals(False)

    def _ai_pin_selected(self):
        model = self._ai_model_combo.currentText().strip()
        if not model or model.startswith("Could not") or model.startswith("Loading"):
            return
        try:
            from core import ai as omni
            msg = omni.set_override(model)
            self._log_sig.emit(f"SYS: {msg}")
        except Exception as e:
            self._log_sig.emit(f"ERR: AI pin — {e}")
        self._tick_ai()

    def _style_ai_chip(self, manual: bool, battle: bool):
        col = "#ff9d00" if manual else (C.MUTED_C if battle else C.PRI)
        bg  = "#1a1000" if manual else ("#140006" if battle else C.PRI_GHO)
        self._ai_chip.setStyleSheet(
            f"QPushButton {{ background: {bg}; color: {col};"
            f"border: 1px solid {col}; border-radius: 4px; padding: 0 10px; }}")

    def _style_ai_override_btns(self, active_key):
        for key, b in self._ai_ovr_btns.items():
            on = (key == active_key)
            col = "#ff9d00" if (on and key) else (C.PRI if on else C.TEXT_MED)
            b.setStyleSheet(f"""
                QPushButton {{ background: {"#1a1000" if (on and key) else (C.PRI_GHO if on else C.PANEL2)};
                    color: {col}; border: 1px solid {col if on else C.BORDER};
                    border-radius: 5px; padding: 0 12px; }}
                QPushButton:hover {{ border: 1px solid {C.PRI}; color: {C.PRI}; }}
            """)

    def _ai_set_override(self, key):
        try:
            from core import ai as omni
            msg = omni.set_override(key)
            self._log_sig.emit(f"SYS: {msg}")
        except Exception as e:
            self._log_sig.emit(f"ERR: AI override — {e}")
        self._tick_ai()

    def _tick_ai(self):
        try:
            from core import ai as omni
            st, ovr, battle = omni.status(), omni.get_override(), omni.battle_mode()
        except Exception:
            return
        # header chip — always updated, cheap
        served = st.get("served") or st.get("model") or "AUTO"
        short = served.split("/")[-1][:22]
        self._ai_chip.setText(f"◈ AI  {short}" + ("  ⚔" if battle else ""))
        self._style_ai_chip(manual=bool(ovr), battle=battle)

        if self._pages.currentIndex() != 5:
            return   # full panel refresh only when visible

        self._ai_model_lbl.setText(served)
        self._ai_provider_lbl.setText(
            f"provider: {st.get('provider', '—')} · bucket: {st.get('model', '—')} "
            f"· task: {st.get('task', '—')}")
        if battle:
            self._ai_mode_lbl.setText(" ⚔ BATTLE MODE ")
            self._ai_mode_lbl.setStyleSheet(
                f"color: {C.MUTED_C}; background: #140006; border: 1px solid {C.MUTED_C};"
                "border-radius: 3px;")
        else:
            self._ai_mode_lbl.setText(" NORMAL MODE ")
            self._ai_mode_lbl.setStyleSheet(
                f"color: {C.PRI}; background: {C.PRI_GHO}; border: 1px solid {C.PRI_DIM};"
                "border-radius: 3px;")
        if ovr:
            self._ai_auto_lbl.setText(f" MANUAL · {ovr.split('/')[-1]} ")
            self._ai_auto_lbl.setStyleSheet(
                "color: #ff9d00; background: #1a1000; border: 1px solid #ff9d00;"
                "border-radius: 3px;")
        else:
            self._ai_auto_lbl.setText(" AUTO ")
            self._ai_auto_lbl.setStyleSheet(
                f"color: {C.PRI}; background: {C.PRI_GHO}; border: 1px solid {C.PRI_DIM};"
                "border-radius: 3px;")
        gw = getattr(self, "_ai_gw_ok", None)
        self._ai_health_dot.setText(
            "● OMNIROUTE ONLINE" if gw else
            ("● OMNIROUTE OFFLINE" if gw is False else "● checking…"))
        self._ai_health_dot.setStyleSheet(
            f"color: {C.GREEN if gw else (C.MUTED_C if gw is False else C.TEXT_DIM)};"
            "background: transparent; border: none;")
        lat = st.get("latency")
        self._ai_latency_lbl.setText(
            f"latency: {lat}s · last request: {st.get('ts', '—')} · "
            f"state: {st.get('state', 'idle')}")
        self._ai_reason_lbl.setText(st.get("reason", "Waiting for the first AI request…"))
        conf = st.get("confidence")
        self._ai_conf_lbl.setText(f"confidence: {conf}%" if conf else "confidence: —")
        try:
            chain = [omni.pick_model(st.get("task", "chat"))] + \
                    [m for m in omni.FALLBACK_CHAIN
                     if m != omni.pick_model(st.get("task", "chat"))]
            self._ai_chain_lbl.setText("fallback: " + "  →  ".join(
                m.split("/")[-1] for m in chain))
        except Exception:
            pass
        # live routing flash — served model changed since last look
        if served not in (None, "AUTO") and self._ai_last_served \
                and served != self._ai_last_served:
            self._ai_model_lbl.setStyleSheet(
                f"color: {C.BG}; background: {C.PRI}; border-radius: 4px;")
            QTimer.singleShot(450, lambda: self._ai_model_lbl.setStyleSheet(
                f"color: {C.WHITE}; background: transparent; border: none;"))
        self._ai_last_served = served if served != "AUTO" else self._ai_last_served
        self._style_ai_override_btns(ovr and next(
            (k for k, v in omni.OVERRIDES.items() if v == ovr), ovr) or None)
        # history table
        rows = []
        for ev in omni.history(10):
            t = (ev.get("ts") or "")[-8:]
            ok = "<span style='color:%s'>✓</span>" % C.GREEN if ev.get("ok") \
                else "<span style='color:%s'>✗</span>" % C.MUTED_C
            rows.append(
                f"<tr><td>{t}&nbsp;&nbsp;</td>"
                f"<td>{(ev.get('served') or ev.get('model', '')).split('/')[-1]}"
                f"&nbsp;&nbsp;</td><td>{ev.get('task', '')}&nbsp;&nbsp;</td>"
                f"<td>{ev.get('latency', '')}s&nbsp;&nbsp;</td><td>{ok}</td></tr>")
        if rows:
            self._ai_hist_lbl.setText(
                "<table style='color:%s'>%s</table>" % (C.TEXT_MED, "".join(rows)))

    def _ai_health_worker(self):
        # ponytail: 20s poll thread; per-provider health lives inside OmniRoute
        while True:
            try:
                from core import ai as omni
                self._ai_gw_ok = omni.available()
            except Exception:
                self._ai_gw_ok = False
            time.sleep(20)

    # ── IMAGE STUDIO page ────────────────────────────────────────────────
    def _build_image_page(self) -> QWidget:
        from PyQt6.QtWidgets import QComboBox
        page = QWidget()
        lay = QVBoxLayout(page)
        lay.setContentsMargins(14, 12, 14, 12); lay.setSpacing(10)

        gen, gl = self._panel("◈ IMAGE GENERATION — FLUX via OmniRoute")
        row = QHBoxLayout(); row.setSpacing(6)
        self._img_prompt = QLineEdit()
        self._img_prompt.setPlaceholderText("Describe the image… e.g. 'minimal arc-reactor logo, dark blue neon'")
        self._img_prompt.setFixedHeight(30)
        self._img_prompt.setFont(QFont("Courier New", 9))
        self._img_prompt.setStyleSheet(
            f"background: {C.PANEL2}; color: {C.WHITE}; border: 1px solid {C.BORDER};"
            "border-radius: 5px; padding: 0 10px;")
        self._img_prompt.returnPressed.connect(self._img_generate)
        self._img_size = QComboBox()
        self._img_size.addItems(["1024x1024", "1024x576", "576x1024"])
        self._img_size.setFixedHeight(30)
        self._img_size.setFont(QFont("Courier New", 8))
        self._img_size.setStyleSheet(
            f"QComboBox {{ background: {C.PANEL2}; color: {C.WHITE};"
            f"border: 1px solid {C.BORDER}; border-radius: 5px; padding: 0 8px; }}"
            f"QComboBox QAbstractItemView {{ background: {C.PANEL}; color: {C.WHITE}; }}")
        self._img_btn = QPushButton("⚡ GENERATE")
        self._img_btn.setFixedHeight(30)
        self._img_btn.setFont(QFont("Courier New", 9, QFont.Weight.Bold))
        self._img_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._img_btn.setStyleSheet(
            f"QPushButton {{ background: {C.PRI_GHO}; color: {C.PRI};"
            f"border: 1px solid {C.PRI_DIM}; border-radius: 5px; padding: 0 14px; }}"
            f"QPushButton:hover {{ border: 1px solid {C.PRI}; }}"
            f"QPushButton:disabled {{ color: {C.TEXT_DIM}; border: 1px solid {C.BORDER}; }}")
        self._img_btn.clicked.connect(self._img_generate)
        row.addWidget(self._img_prompt, stretch=1)
        row.addWidget(self._img_size); row.addWidget(self._img_btn)
        gl.addLayout(row)
        self._img_status = QLabel("Describe an image and hit GENERATE — or just tell me by voice.")
        self._img_status.setFont(QFont("Courier New", 8))
        self._img_status.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent; border: none;")
        gl.addWidget(self._img_status)
        lay.addWidget(gen)

        prev, pl = self._panel("◈ PREVIEW")
        self._img_preview = QLabel("—")
        self._img_preview.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._img_preview.setMinimumHeight(320)
        self._img_preview.setStyleSheet(
            f"background: {C.PANEL2}; border: 1px solid {C.BORDER}; border-radius: 6px;"
            f"color: {C.TEXT_DIM};")
        pl.addWidget(self._img_preview, stretch=1)
        lay.addWidget(prev, stretch=1)

        hist, hl = self._panel("◈ HISTORY — Studios/Images")
        self._img_gallery = QHBoxLayout(); self._img_gallery.setSpacing(6)
        gw = QWidget(); gw.setLayout(self._img_gallery)
        gw.setStyleSheet("background: transparent; border: none;")
        hl.addWidget(gw)
        lay.addWidget(hist)

        self._img_current: str | None = None
        self._refresh_img_gallery()
        return page

    def _images_dir(self) -> Path:
        d = BASE_DIR / "Studios" / "Images"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _img_generate(self):
        prompt = self._img_prompt.text().strip()
        if not prompt:
            return
        self._img_btn.setEnabled(False)
        self._img_status.setText(f"Generating: {prompt[:70]} …")
        size = self._img_size.currentText()

        def _work():
            try:
                from core.ai import generate_image
                _, path = generate_image(prompt, size=size)
                self._img_done_sig.emit((True, str(path)))
            except Exception as e:
                self._img_done_sig.emit((False, str(e)[:160]))
        threading.Thread(target=_work, daemon=True, name="ImgGen").start()

    def _on_img_done(self, payload):
        ok, data = payload
        self._img_btn.setEnabled(True)
        if not ok:
            self._img_status.setText(f"Failed: {data}")
            return
        self._img_status.setText(f"Saved → {Path(data).name}")
        self._show_img(data)
        self._refresh_img_gallery()

    def _show_img(self, path: str):
        self._img_current = path
        pm = QPixmap(path)
        if not pm.isNull():
            self._img_preview.setPixmap(pm.scaled(
                self._img_preview.width(), max(320, self._img_preview.height()),
                Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation))

    def _refresh_img_gallery(self):
        while self._img_gallery.count():
            it = self._img_gallery.takeAt(0)
            if it.widget():
                it.widget().deleteLater()
        files = sorted(self._images_dir().glob("img_*.*"),
                       key=lambda p: p.stat().st_mtime, reverse=True)[:6]
        for f in files:
            b = QPushButton()
            b.setFixedSize(84, 84)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            pm = QPixmap(str(f))
            if not pm.isNull():
                b.setIcon(QIcon(pm.scaled(80, 80, Qt.AspectRatioMode.KeepAspectRatio,
                                          Qt.TransformationMode.SmoothTransformation)))
                b.setIconSize(QSize(80, 80))
            b.setStyleSheet(
                f"QPushButton {{ background: {C.PANEL2}; border: 1px solid {C.BORDER};"
                "border-radius: 6px; }"
                f"QPushButton:hover {{ border: 1px solid {C.PRI}; }}")
            b.clicked.connect(lambda _, p=str(f): self._show_img(p))
            self._img_gallery.addWidget(b)
        self._img_gallery.addStretch()

    def _open_image_studio(self, path: str):
        """Voice-tool entry: jump to the studio showing a fresh image."""
        self._switch_page(6, "IMAGE STUDIO")
        if path:
            self._show_img(path)
            self._img_status.setText(f"Saved → {Path(path).name}")
            self._refresh_img_gallery()
        self._show_from_anywhere()

    # ── dock ─────────────────────────────────────────────────────────────
    def _build_dock(self) -> QWidget:
        w = QWidget(); w.setFixedHeight(52)
        w.setStyleSheet(f"background: {C.DARK}; border-top: 1px solid {C.BORDER};")
        lay = QHBoxLayout(w); lay.setContentsMargins(14, 4, 14, 4); lay.setSpacing(6)
        lay.addStretch()
        apps = [("🗂", "Explorer",  lambda: _launch("explorer")),
                ("🔎", "Search",    lambda: self._input.setFocus()),
                ("🧑‍💻", "VS Code",  lambda: _launch("code")),
                ("🌐", "Chrome",    lambda: _launch("chrome")),
                ("🎵", "Spotify",   lambda: _launch("spotify:")),
                ("💬", "WhatsApp",  lambda: _launch("whatsapp:")),
                ("▶", "YouTube",    lambda: __import__("webbrowser").open("https://youtube.com")),
                ("🖥", "Terminal",  lambda: _launch("cmd" if _OS == "Windows" else "x-terminal-emulator"))]
        for icon, tip, cb in apps:
            b = QPushButton(icon); b.setFixedSize(40, 40); b.setToolTip(tip)
            b.setCursor(Qt.CursorShape.PointingHandCursor)
            b.setStyleSheet(f"""
                QPushButton {{ background: {C.PANEL}; border: 1px solid {C.BORDER};
                    border-radius: 8px; font-size: 15px; }}
                QPushButton:hover {{ border: 1px solid {C.PRI}; background: {C.PRI_GHO}; }}
            """)
            b.clicked.connect(cb)
            lay.addWidget(b)
        lay.addStretch()
        lbl = QLabel("[F4] Sleep · [F11] Fullscreen · © FATIHMAKES")
        lbl.setFont(QFont("Courier New", 7))
        lbl.setStyleSheet(f"color: {C.TEXT_DIM}; background: transparent;")
        lay.addWidget(lbl)
        return w

    # ── quick actions ────────────────────────────────────────────────────
    def _qa_screenshot(self):
        def _shot():
            try:
                import mss
                out = Path.home() / "Pictures" / "Jarvis"
                out.mkdir(parents=True, exist_ok=True)
                path = out / time.strftime("shot_%Y%m%d_%H%M%S.png")
                with mss.mss() as sct:
                    sct.shot(mon=1, output=str(path))
                self._log_sig.emit(f"FILE: Screenshot saved — {path}")
            except Exception as e:
                self._log_sig.emit(f"ERR: screenshot — {e}")
        threading.Thread(target=_shot, daemon=True).start()

    def _qa_record(self):
        rec = self._recorder.toggle()
        self._qa_btns["⏺ Record"].setText("⏹ Stop rec" if rec else "⏺ Record")

    def _qa_clear_temp(self):
        def _clean():
            freed, n = 0, 0
            root = Path(tempfile.gettempdir())
            for f in root.glob("**/*"):
                try:
                    if f.is_file():
                        sz = f.stat().st_size
                        f.unlink(); freed += sz; n += 1
                except Exception:
                    continue  # locked by another process — normal
            self._log_sig.emit(f"SYS: Temp cleaned — {n} files, {freed/1e6:.1f} MB freed.")
        threading.Thread(target=_clean, daemon=True).start()

    def _media_key(self, key: str):
        def _press():
            try:
                import pyautogui
                pyautogui.press(key)
            except Exception as e:
                self._log_sig.emit(f"ERR: media key — {e}")
        threading.Thread(target=_press, daemon=True).start()

    # ── dynamic data ─────────────────────────────────────────────────────
    def _refresh_project(self):
        try:
            mem = json.loads((BASE_DIR / "memory" / "long_term.json").read_text(encoding="utf-8"))
            projects = mem.get("projects", {})
            lines = []
            for k, e in list(projects.items())[:3]:
                v = e.get("value") if isinstance(e, dict) else e
                if v:
                    lines.append(f"▸ {k.replace('_', ' ').title()}\n   {v}")
            self._proj_lbl.setText("\n".join(lines) if lines else "No active projects yet")
        except Exception:
            self._proj_lbl.setText("No active projects yet")

    def _refresh_events(self):
        ev = _load_events()
        today = time.strftime("%Y-%m-%d")
        upcoming = sorted((d, t) for d, ts in ev.items() for t in ts if d >= today)[:4]
        if upcoming:
            self._events_lbl.setText("\n".join(f"▸ {d}  {t}" for d, t in upcoming))
        else:
            self._events_lbl.setText("No events — add them in CALENDAR")
        if hasattr(self, "_ev_list"):
            self._ev_list.setText("\n".join(f"{d} — {t}" for d, t in upcoming))

    def _add_event(self):
        txt = self._ev_input.text().strip()
        if not txt:
            return
        date = self._cal.selectedDate().toString("yyyy-MM-dd")
        ev = _load_events()
        ev.setdefault(date, []).append(txt)
        _save_events(ev)
        self._ev_input.clear()
        self._log_sig.emit(f"SYS: Event added {date} — {txt}")
        self._refresh_events()

    def _refresh_suggestions(self):
        # clear old buttons
        while self._sug_lay.count() > 1:
            item = self._sug_lay.takeAt(1)
            if item.widget():
                item.widget().deleteLater()
        sugs = []
        try:
            bat = psutil.sensors_battery()
            if bat and not bat.power_plugged and bat.percent < 35:
                sugs.append((f"🔋 Battery {bat.percent:.0f}% — plug in", None))
            if psutil.disk_usage(os.path.abspath(os.sep)).percent > 85:
                sugs.append(("🧹 Disk almost full — clear temp files", "clear temp files"))
        except Exception:
            pass
        hour = time.localtime().tm_hour
        if hour >= 23 or hour < 5:
            sugs.append(("🌙 It's late — consider wrapping up", None))
        ev = _load_events()
        today = time.strftime("%Y-%m-%d")
        for t in ev.get(today, [])[:2]:
            sugs.append((f"📅 Today: {t}", None))
        if not sugs:
            sugs = [("💡 Ask me to automate something you repeat daily", None),
                    ("🗞 Want today's news?", "give me today's news")]
        for text, cmd in sugs[:4]:
            b = QPushButton(text)
            b.setFixedHeight(26)
            b.setFont(QFont("Courier New", 7))
            b.setCursor(Qt.CursorShape.PointingHandCursor if cmd else Qt.CursorShape.ArrowCursor)
            b.setStyleSheet(f"""
                QPushButton {{ background: transparent; color: {C.TEXT};
                    border: 1px solid {C.BORDER}; border-radius: 4px; text-align: left;
                    padding-left: 8px; }}
                QPushButton:hover {{ border: 1px solid {C.PRI}; }}
            """)
            if cmd:
                b.clicked.connect(lambda _, c=cmd: self._send_cmd(c))
            self._sug_lay.addWidget(b)
        self._sug_lay.addStretch()

    # ── command / state plumbing ─────────────────────────────────────────
    def _send_from(self, box: QLineEdit):
        txt = box.text().strip()
        if not txt:
            return
        box.clear()
        self._send_cmd(txt)

    def _send_cmd(self, text: str):
        self._log.append_log(f"You: {text}")
        self._push_convo(f"You: {text}")
        if self.on_text_command:
            threading.Thread(target=self.on_text_command, args=(text,), daemon=True).start()

    _convo: list[str] = []

    def _push_convo(self, line: str):
        self._convo.append(f"{time.strftime('%H:%M')}  {line[:64]}")
        self._convo = self._convo[-6:]
        self._convo_lbl.setText("\n".join(self._convo))

    def _on_log(self, text: str):
        if self._persona == "friday":
            text = text.replace("JARVIS", "FRIDAY").replace("Jarvis", "Friday")
        self._log.append_log(text)
        if text.startswith(("You:", "Jarvis:", "Friday:")):
            self._push_convo(text)

    def _apply_state(self, state: str):
        self.hud.state    = state
        self.hud.speaking = (state == "SPEAKING")
        self._wave.state  = state
        if getattr(self, "_deck", None) is not None:
            self._deck.set_state(state)
        if self._persona == "friday":
            self._friday_state_controller.update_from_runtime(state)
            bridge = getattr(self, "_friday_bridge", None)
            if bridge is not None:
                bridge.on_runtime_state(state)

    def _set_mic_overlay_visible(self, visible: bool):
        """Show/hide the compact mic pill (lifecycle OPEN_MIC / CLOSE_MIC).

        Deliberately does not open the dashboard: mic-only mode should take
        minimal screen space. The overlay is a view; muting still flows
        through the one _set_muted path.
        """
        if visible:
            if self._mic_overlay is None:
                from launcher.mic_overlay import MicOverlay
                self._mic_overlay = MicOverlay()
                self._mic_overlay.mute_requested.connect(self._toggle_mute)
                self._mic_overlay.close_requested.connect(
                    lambda: self._set_mic_overlay_visible(False))
            self._mic_overlay.set_assistant(
                "FRIDAY" if self._persona == "friday" else "JARVIS")
            self._mic_overlay.set_state("LISTENING", self._muted)
            self._mic_overlay.show()
            self._mic_overlay.raise_()
        elif self._mic_overlay is not None:
            self._mic_overlay.hide()

    def _set_input_audio_level(self, level: float):
        if getattr(self, "_deck", None) is not None:
            self._deck.set_mic_level(level)
        bridge = getattr(self, "_friday_bridge", None)
        if bridge is not None:
            bridge.on_mic_level(level)
        if self._mic_overlay is not None and self._mic_overlay.isVisible():
            self._mic_overlay.push_level(level)

    def _toggle_mute(self):
        self._set_muted(not self._muted)

    def _set_muted(self, value: bool):
        if value == self._muted:
            return
        # every app-side mute passes through here — if the mic ever mutes
        # without this line in the log, the cause is outside the app
        print(f"[Audio] Mute state -> {'MUTED (sleep)' if value else 'ACTIVE'}")
        self._muted = value
        self.hud.muted = self._muted
        if getattr(self, "_deck", None) is not None:
            self._deck.face.muted = self._muted
        bridge = getattr(self, "_friday_bridge", None)
        if bridge is not None:
            bridge.on_mic_muted(self._muted)
        if self._mic_overlay is not None and self._mic_overlay.isVisible():
            self._mic_overlay.set_state("LISTENING", self._muted)
        self._style_header_state()
        # The assistant's own name, so FRIDAY never reports herself as JARVIS.
        who = "FRIDAY" if self._persona == "friday" else "JARVIS"
        wake = "Friday" if self._persona == "friday" else "Jarvis"
        if self._muted:
            self._apply_state("MUTED")
            self._nav_status.setText(f"◉ {who} ASLEEP")
            self._nav_status.setStyleSheet(
                f"color: {C.MUTED_C}; background: transparent; padding: 8px;")
            self._log.append_log(f"SYS: Asleep — say '{wake}' to wake me.")
        else:
            self._apply_state("LISTENING")
            self._nav_status.setText(f"◉ {who} ONLINE")
            self._nav_status.setStyleSheet(
                f"color: {C.GREEN}; background: transparent; padding: 8px;")
            self._log.append_log("SYS: Awake. Listening.")

    def _toggle_fullscreen(self):
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def _tick_clock(self):
        self._clock_lbl.setText(time.strftime("%H:%M:%S"))
        self._date_lbl.setText(time.strftime("%A, %d %B %Y"))

    def _ease_gauges(self):
        for g in (self._g_cpu, self._g_ram, self._g_gpu, self._g_disk):
            g.tick()

    def _update_metrics(self):
        snap = _metrics.snapshot()
        try:
            snap["disk"] = psutil.disk_usage(os.path.abspath(os.sep)).percent
        except Exception:
            snap["disk"] = None
        try:
            battery = psutil.sensors_battery()
            snap["battery"] = battery.percent if battery is not None else None
            snap["charging"] = battery.power_plugged if battery is not None else None
        except Exception:
            snap["battery"] = None; snap["charging"] = None
        if getattr(self, "_deck", None) is not None:
            self._deck.set_metrics(snap)
        self._g_cpu.set_value(snap["cpu"])
        self._g_ram.set_value(snap["mem"])
        self._g_gpu.set_value(max(0, snap["gpu"]),
                              "" if snap["gpu"] >= 0 else "N/A")
        try:
            self._g_disk.set_value(snap["disk"] or 0)
        except Exception:
            pass

        try:
            bat = psutil.sensors_battery()
        except Exception:
            bat = None
        if bat is not None:
            plug = "⚡" if bat.power_plugged else ""
            self._bar_bat.set_value(bat.percent, f"{bat.percent:.0f}%{plug}")
        else:
            self._bar_bat.set_value(0, "N/A")

        tmp = snap["tmp"]
        self._bar_tmp.set_value(min(100, tmp) if tmp >= 0 else 0,
                                f"{tmp:.0f}°C" if tmp >= 0 else "N/A")
        net = snap["net"]
        self._bar_net.set_value(min(100, net * 10),
                                f"{net*1024:.0f}KB/s" if net < 1 else f"{net:.1f}MB/s")
        try:
            up = time.time() - psutil.boot_time()
            self._bar_up.set_value(min(100, up / 864),
                                   f"{int(up//3600):02d}h {int(up%3600//60):02d}m")
        except Exception:
            self._bar_up.set_value(0, "--")

        # slow refreshers piggyback here (~every 30s)
        if int(time.time()) % 30 < 2:
            self._refresh_suggestions()
            self._refresh_events()
            self._refresh_project()

    # ── files / setup (unchanged behavior) ───────────────────────────────
    def _on_file_selected(self, path: str):
        self._current_file = path
        p    = Path(path)
        cat  = _file_category(p)
        icon, _ = _FILE_ICONS.get(cat, _FILE_ICONS["unknown"])
        size = _fmt_size(p.stat().st_size)
        self._file_hint.setText(f"{icon}  {p.name}  ·  {size}  ·  Tell JARVIS what to do with it")
        self._log.append_log(f"FILE: {p.name} ({size}) loaded")

    def _check_config(self) -> bool:
        if not API_FILE.exists():
            return False
        try:
            d = json.loads(API_FILE.read_text(encoding="utf-8"))
            return bool(d.get("gemini_api_key")) and bool(d.get("os_system"))
        except Exception:
            return False

    def _show_setup(self):
        ov = SetupOverlay(self.centralWidget())
        cw = self.centralWidget()
        ow, oh = 460, 390
        ov.setGeometry((cw.width() - ow) // 2, (cw.height() - oh) // 2, ow, oh)
        ov.done.connect(self._on_setup_done)
        ov.show()
        self._overlay = ov

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._overlay and self._overlay.isVisible():
            ow, oh = 460, 390
            cw = self.centralWidget()
            self._overlay.setGeometry((cw.width() - ow) // 2,
                                      (cw.height() - oh) // 2, ow, oh)

    def _on_setup_done(self, key: str, os_name: str):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        cfg = {}
        try:
            cfg = json.loads(API_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
        cfg["gemini_api_key"] = key
        cfg["os_system"]      = os_name
        API_FILE.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        self._ready = True
        if self._overlay:
            self._overlay.hide()
            self._overlay = None
        self._apply_state("LISTENING")
        self._log.append_log(f"SYS: Initialised. OS={os_name.upper()}. JARVIS online.")

class _RootShim:
    def __init__(self, app: QApplication):
        self._app = app
    def mainloop(self):
        self._app.exec()
    def protocol(self, *_):
        pass


class JarvisUI:
    def __init__(self, face_path: str, size=None):
        self._app = QApplication.instance() or QApplication(sys.argv)
        self._app.setStyle("Fusion")
        self._app.setQuitOnLastWindowClosed(False)   # tray keeps us alive
        self._win = MainWindow(face_path)
        if "--hidden" in sys.argv:
            self._win.hide()      # boot silently to tray; wake word shows us
        else:
            self._win.show()
        self.root = _RootShim(self._app)

    def show_window(self):
        """Thread-safe: instantly show + raise the window from any thread.
        
        This is called when wake word is detected — must be immediate.
        """
        print("[Desktop] show signal emitted — instant wake")
        self._win._show_sig.emit()
        
        # Also trigger the JarvisApp if connected
        jarvis_app = getattr(self._win, "_jarvis_app", None)
        if jarvis_app is not None:
            jarvis_app.activate()

    @property
    def window_visible(self) -> bool:
        return self._win.isVisible() and not self._win.isMinimized()

    @property
    def muted(self) -> bool:
        return self._win._muted

    @muted.setter
    def muted(self, v: bool):
        if v != self._win._muted:
            self._win._toggle_mute()

    def set_muted(self, v: bool):
        """Thread-safe mute/sleep toggle (safe to call from audio thread)."""
        self._win._mute_sig.emit(v)

    @property
    def theme(self) -> str:
        return self._win._theme

    def set_theme(self, name: str):
        """Thread-safe theme switch: 'normal' | 'battle'."""
        self._win._theme_sig.emit(name)

    @property
    def persona(self) -> str:
        return self._win._persona

    def set_persona(self, name: str):
        """Thread-safe persona switch: 'friday' | 'jarvis'."""
        self._win._persona_sig.emit(name)

    def set_emotion(self, name: str):
        """Thread-safe facial emotion (FRIDAY face only; no-op on the orb)."""
        self._win._emotion_sig.emit(name)

    def set_audio_level(self, v: float):
        """Live output-audio loudness 0..1 — drives FRIDAY's speaking effects."""
        try:
            level = max(0.0, min(1.0, float(v)))
            self._win.hud.level = level
            if getattr(self._win, "_deck", None) is not None:
                self._win._deck.face.level = level
            bridge = getattr(self._win, "_friday_bridge", None)
            if bridge is not None:
                bridge.on_tts_level(level)
        except Exception:
            pass

    def set_input_audio_level(self, v: float):
        """Thread-safe microphone RMS for FRIDAY's listening waveform."""
        self._win._input_level_sig.emit(max(0.0, min(1.0, float(v))))

    @property
    def current_file(self) -> str | None:
        return self._win._drop_zone.current_file()

    @property
    def on_text_command(self):
        return self._win.on_text_command

    @on_text_command.setter
    def on_text_command(self, cb):
        self._win.on_text_command = cb

    def set_state(self, state: str):
        self._win._state_sig.emit(state)

    def write_log(self, text: str):
        self._win._log_sig.emit(text)

    def wait_for_api_key(self):
        while not self._win._ready:
            time.sleep(0.1)

    def start_speaking(self):
        self.set_state("SPEAKING")

    def stop_speaking(self):
        if not self.muted:
            self.set_state("LISTENING")
