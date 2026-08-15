"""Phase 3: read-only status, answered from the collectors that already exist.

Nothing here polls the machine itself. FRIDAY already runs one shared
`SystemData` thread and `core.ai` already records its own routing state, so
this module reads those snapshots and formats them. Starting a second poller
for WhatsApp would double the syscalls and the RAM for no new information.

The honesty rule from `friday/data.py` carries over unchanged: a figure that
could not be measured is reported as unavailable, never as a plausible number.
Temperature in particular has no reading on this hardware interface, and says
so rather than guessing.
"""
from __future__ import annotations

import logging
import os
import time

log = logging.getLogger("nexus.status")

#: The exact wording required when no thermal sensor is reachable. psutil's
#: `sensors_temperatures` is not implemented on Windows, and WMI thermal zones
#: are absent on most consumer laptops — so this is the normal answer here, not
#: an error path.
TEMPERATURE_UNAVAILABLE = ("Temperature data is not available from the current "
                           "hardware interface.")

UNAVAILABLE = "Not available"


def _pct(value: float | None) -> str:
    return UNAVAILABLE if value is None else f"{value:.0f}%"


def _reading():
    """The shared FRIDAY snapshot, or None when the collector is unreachable."""
    try:
        from friday.data import system
    except Exception as exc:
        log.debug("system collector unavailable: %s", exc)
        return None
    reading = system.snapshot()
    if reading.cpu is None:
        # The collector polls on a timer, so the first snapshot after boot is
        # empty. Run one synchronous pass rather than reporting a machine that
        # is running as entirely unmeasurable. Same code the thread runs — this
        # does not start a second poller.
        try:
            system._collect()
            reading = system.snapshot()
        except Exception as exc:
            log.debug("priming collector failed: %s", exc)
    return reading


def _persona() -> str:
    """Read the persona from config rather than importing `main`.

    Same source of truth as `main._get_persona()`, but importing main would
    pull the entire voice runtime in just to read one string.
    """
    try:
        import json
        from pathlib import Path
        path = Path(__file__).resolve().parents[1] / "config" / "api_keys.json"
        value = str(json.loads(path.read_text(encoding="utf-8"))
                    .get("persona", "jarvis")).lower()
        return value if value in ("jarvis", "friday") else "jarvis"
    except Exception:
        return "jarvis"


def _uptime_text() -> str:
    try:
        import psutil
        seconds = time.time() - psutil.boot_time()
    except Exception:
        return UNAVAILABLE
    hours, minutes = divmod(int(seconds // 60), 60)
    days, hours = divmod(hours, 24)
    if days:
        return f"{days}d {hours}h"
    return f"{hours}h {minutes}m" if hours else f"{minutes}m"


def temperature() -> tuple[float | None, str]:
    """(celsius, human text). Truthful about having no sensor."""
    try:
        import psutil
        readings = getattr(psutil, "sensors_temperatures", lambda: {})()
    except Exception:
        readings = {}
    for entries in (readings or {}).values():
        for entry in entries:
            current = getattr(entry, "current", None)
            if current:
                return float(current), f"{float(current):.0f}°C"
    return None, TEMPERATURE_UNAVAILABLE


def snapshot() -> dict:
    """Structured status. Every key is either a real value or None."""
    r = _reading()
    celsius, temp_text = temperature()
    data = {
        "host": getattr(r, "host", "") or os.environ.get("COMPUTERNAME", ""),
        "persona": _persona(),
        "uptime": _uptime_text(),
        "cpu_percent": getattr(r, "cpu", None),
        "cpu_name": getattr(r, "cpu_name", ""),
        "ram_percent": getattr(r, "ram", None),
        "ram_detail": getattr(r, "ram_detail", ""),
        "gpu_percent": getattr(r, "gpu", None),
        "gpu_name": getattr(r, "gpu_name", ""),
        "storage_percent": getattr(r, "storage", None),
        "storage_detail": getattr(r, "storage_detail", ""),
        "battery_percent": getattr(r, "battery", None),
        "battery_plugged": getattr(r, "battery_plugged", None),
        "net_online": getattr(r, "net_online", None),
        "net_down_mbps": getattr(r, "net_down_mbps", None),
        "net_up_mbps": getattr(r, "net_up_mbps", None),
        "temperature_celsius": celsius,
        "temperature_text": temp_text,
    }
    return data


def format_status(data: dict | None = None) -> str:
    """A compact, WhatsApp-shaped summary. Neutral tone: NEXUS has no persona."""
    d = data if data is not None else snapshot()
    lines = [f"Laptop status — {d.get('host') or 'this machine'}"]

    cpu = _pct(d.get("cpu_percent"))
    name = d.get("cpu_name")
    lines.append(f"CPU: {cpu}" + (f" ({name})" if name else ""))

    ram = _pct(d.get("ram_percent"))
    detail = d.get("ram_detail")
    lines.append(f"RAM: {ram}" + (f" ({detail})" if detail else ""))

    if d.get("gpu_percent") is None:
        lines.append("GPU: Not available")
    else:
        gpu_name = d.get("gpu_name")
        lines.append(f"GPU: {_pct(d['gpu_percent'])}"
                     + (f" ({gpu_name})" if gpu_name else ""))

    storage = _pct(d.get("storage_percent"))
    sdetail = d.get("storage_detail")
    lines.append(f"Disk: {storage} used" + (f" ({sdetail})" if sdetail else ""))

    battery = d.get("battery_percent")
    if battery is None:
        lines.append("Battery: Not available")
    else:
        plugged = d.get("battery_plugged")
        state = "charging" if plugged else "on battery" if plugged is False else ""
        lines.append(f"Battery: {battery:.0f}%" + (f" ({state})" if state else ""))

    online = d.get("net_online")
    if online is None:
        lines.append("Network: Not available")
    else:
        rate = ""
        down, up = d.get("net_down_mbps"), d.get("net_up_mbps")
        if down is not None and up is not None:
            rate = f" — {down:.1f} down / {up:.1f} up Mbps"
        lines.append(("Network: online" if online else "Network: offline") + rate)

    # The unavailable text is a full sentence, so it reads as its own line
    # rather than as the value of a "Temperature:" label.
    temp_text = d.get("temperature_text") or TEMPERATURE_UNAVAILABLE
    lines.append(temp_text if temp_text == TEMPERATURE_UNAVAILABLE
                 else f"Temperature: {temp_text}")
    lines.append(f"Uptime: {d.get('uptime', UNAVAILABLE)}")
    lines.append(f"Active assistant: {str(d.get('persona', '')).upper()}")
    return "\n".join(lines)


def whats_happening() -> str:
    """What the assistant is doing right now, from core.ai's own record."""
    try:
        from friday.data import ai_reading
        reading = ai_reading()
    except Exception as exc:
        log.debug("ai reading unavailable: %s", exc)
        return "The assistant is running, but its routing state is not readable."

    persona = _persona().upper()
    if not reading.state and not reading.model:
        return (f"{persona} is idle — no request has been routed since the "
                f"assistant started.")

    parts = [f"{persona} is {reading.state or 'idle'}"]
    if reading.task:
        parts.append(f"task: {reading.task}")
    if reading.model:
        served = f" via {reading.served}" if reading.served else ""
        parts.append(f"model: {reading.model}{served}")
    if reading.latency_ms is not None:
        parts.append(f"last reply took {reading.latency_ms} ms")
    parts.append(f"routing: {reading.routing}")
    return ". ".join(parts) + "."
