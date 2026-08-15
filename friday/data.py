"""Real values for FRIDAY's panels.

Every reader here returns ``None`` when a figure genuinely is not knowable on
this machine.  Nothing invents a plausible number: the panels render "Not
available" instead, because a dashboard that guesses is worse than one that
admits ignorance.

Readings are cached briefly so that several panels polling the same figure do
not each pay for a syscall.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger("friday.data")

try:
    import psutil
except Exception:  # pragma: no cover - psutil is a hard dependency in practice
    psutil = None

REPO_ROOT = Path(__file__).resolve().parent.parent

#: Windows needs this so probing subprocesses never flash a console window.
_NO_WINDOW = 0x08000000 if sys.platform == "win32" else 0


@dataclass
class SystemReading:
    """A snapshot of the machine. ``None`` means 'could not be measured'."""

    cpu: float | None = None
    cpu_name: str = ""
    ram: float | None = None
    ram_detail: str = ""
    gpu: float | None = None
    gpu_name: str = ""
    storage: float | None = None
    storage_detail: str = ""
    battery: float | None = None
    battery_plugged: bool | None = None
    net_down_mbps: float | None = None
    net_up_mbps: float | None = None
    net_online: bool | None = None
    host: str = ""


class SystemData:
    """Polls psutil on a background thread; the UI only reads the snapshot.

    The UI thread never blocks on a syscall, which is what keeps FRIDAY
    responsive while an AI call is in flight.
    """

    def __init__(self, interval: float = 2.0):
        self._interval = interval
        self._lock = threading.Lock()
        self._reading = SystemReading()
        self._prev_net = None
        self._prev_net_t = 0.0
        self._gpu_available = True
        self._static_done = False
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="FridaySystemData")
        self._thread.start()

    def snapshot(self) -> SystemReading:
        with self._lock:
            return self._reading

    def stop(self) -> None:
        self._stop.set()

    # ---- collection ----------------------------------------------------

    def _loop(self) -> None:
        if psutil is not None:
            psutil.cpu_percent(interval=None)   # prime the counter
        while not self._stop.wait(self._interval):
            try:
                self._collect()
            except Exception as exc:            # A monitor must never die.
                log.debug("FRIDAY system poll failed: %s", exc)

    def _collect(self) -> None:
        r = SystemReading()
        if psutil is None:
            with self._lock:
                self._reading = r
            return

        r.host = socket.gethostname()
        r.cpu = psutil.cpu_percent(interval=None)
        r.cpu_name = self._cpu_name()

        vm = psutil.virtual_memory()
        r.ram = vm.percent
        r.ram_detail = f"{vm.used / 1e9:.1f} / {vm.total / 1e9:.0f} GB"

        try:
            du = shutil.disk_usage(os.path.abspath(os.sep))
            r.storage = du.used / du.total * 100.0
            r.storage_detail = f"{du.used / 1e12:.2f} / {du.total / 1e12:.2f} TB"
        except Exception:
            pass

        bat = psutil.sensors_battery()
        if bat is not None:
            r.battery = bat.percent
            r.battery_plugged = bat.power_plugged

        try:
            nc = psutil.net_io_counters()
            now = time.monotonic()
            if self._prev_net is not None:
                dt = now - self._prev_net_t
                if dt > 0:
                    r.net_down_mbps = (nc.bytes_recv - self._prev_net.bytes_recv) * 8 / dt / 1e6
                    r.net_up_mbps = (nc.bytes_sent - self._prev_net.bytes_sent) * 8 / dt / 1e6
            else:
                # First pass has no baseline to difference against. Report zero
                # rather than "unavailable": the counters do work here.
                r.net_down_mbps = 0.0
                r.net_up_mbps = 0.0
            self._prev_net, self._prev_net_t = nc, now
        except Exception:
            pass

        r.net_online = self._online()
        r.gpu, r.gpu_name = self._gpu()

        with self._lock:
            self._reading = r

    def _cpu_name(self) -> str:
        cached = getattr(self, "_cpu_name_cache", None)
        if cached is not None:
            return cached
        name = ""
        if sys.platform == "win32":
            name = os.environ.get("PROCESSOR_IDENTIFIER", "").split(",")[0].strip()
        if not name and psutil is not None:
            cores = psutil.cpu_count(logical=True)
            name = f"{cores} logical cores" if cores else ""
        self._cpu_name_cache = name
        return name

    def _gpu(self) -> tuple[float | None, str]:
        """Query nvidia-smi once; a machine without it will not grow one."""
        if not self._gpu_available:
            return None, getattr(self, "_gpu_name_cache", "")
        try:
            out = subprocess.run(
                ["nvidia-smi",
                 "--query-gpu=utilization.gpu,name",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=2.5,
                creationflags=_NO_WINDOW)
            line = out.stdout.strip().splitlines()[0]
            util, name = line.split(",", 1)
            self._gpu_name_cache = name.strip()
            return float(util.strip()), name.strip()
        except Exception:
            self._gpu_available = False
            log.info("FRIDAY: no NVIDIA GPU telemetry on this machine")
            return None, ""

    def _online(self) -> bool | None:
        """Cheap reachability check, refreshed at most every 15 seconds."""
        now = time.monotonic()
        if now - getattr(self, "_online_t", -99) < 15.0:
            return getattr(self, "_online_cache", None)
        try:
            with socket.create_connection(("1.1.1.1", 53), timeout=1.5):
                result = True
        except Exception:
            result = False
        self._online_cache, self._online_t = result, now
        return result


# ---- AI routing ----------------------------------------------------------

@dataclass
class AIReading:
    provider: str | None = None
    model: str | None = None
    served: str | None = None
    routing: str = "Automatic"
    reason: str | None = None
    latency_ms: int | None = None
    state: str | None = None
    task: str | None = None
    streaming: bool = False
    override: str | None = None
    history: list = field(default_factory=list)


def ai_reading() -> AIReading:
    """Current routing state, read from core.ai's own status record.

    Returns an empty reading (all ``None``) when the AI layer has not yet made
    a request, so the panel can say "waiting for first request" honestly.
    """
    reading = AIReading()
    try:
        from core import ai
    except Exception as exc:
        log.debug("FRIDAY: core.ai unavailable: %s", exc)
        return reading

    try:
        status = ai.status()
        reading.provider = status.get("provider") or None
        reading.model = status.get("model") or None
        reading.served = status.get("served") or None
        reading.state = status.get("state") or None
        reading.task = status.get("task") or None
        reading.reason = status.get("reason") or None
        # core.ai records `latency` in seconds (see _set_status in core/ai.py).
        latency = status.get("latency")
        reading.latency_ms = int(float(latency) * 1000) if latency else None
        reading.streaming = status.get("state") == "routing"
    except Exception as exc:
        log.debug("FRIDAY: ai.status() failed: %s", exc)

    try:
        reading.override = ai.get_override()
        reading.routing = f"Manual → {reading.override}" if reading.override else "Automatic"
    except Exception:
        pass

    try:
        reading.history = ai.history(6)
    except Exception:
        reading.history = []

    return reading


def set_ai_override(name: str | None) -> str:
    """Pin routing to a model family, or return to Auto when ``name`` is None."""
    from core import ai
    return ai.set_override(name)


# ---- memory --------------------------------------------------------------

def memory_reading() -> tuple[int | None, str | None]:
    """(entry count, human size) for FRIDAY's long-term memory store."""
    path = REPO_ROOT / "memory" / "long_term.json"
    if not path.is_file():
        return None, None
    try:
        size = path.stat().st_size
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            count = sum(len(v) if isinstance(v, list) else 1 for v in data.values())
        elif isinstance(data, list):
            count = len(data)
        else:
            count = None
        human = f"{size / 1024:.0f} KB" if size < 1e6 else f"{size / 1e6:.1f} MB"
        return count, human
    except Exception as exc:
        log.debug("FRIDAY: memory read failed: %s", exc)
        return None, None


# ---- clipboard -----------------------------------------------------------

def clipboard_preview(limit: int = 120) -> str | None:
    """Current clipboard text, or ``None`` when empty/unavailable."""
    try:
        import pyperclip
        text = (pyperclip.paste() or "").strip()
    except Exception:
        return None
    if not text:
        return None
    text = " ".join(text.split())
    return text[:limit] + ("…" if len(text) > limit else "")


# ---- reminders -----------------------------------------------------------

def reminders() -> list[dict] | None:
    """Scheduled reminders, or ``None`` when the module is not configured."""
    path = REPO_ROOT / "config" / "events.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if isinstance(data, dict):
        items = data.get("reminders") or data.get("events") or []
    else:
        items = data
    return items if isinstance(items, list) else None


# ---- recent tasks --------------------------------------------------------

def recent_tasks(limit: int = 5) -> list[dict]:
    """Real routing events from the metrics log, newest first."""
    try:
        from core import ai
        return ai.history(limit)
    except Exception:
        return []


#: One shared collector; panels read from this rather than starting their own.
system = SystemData()
