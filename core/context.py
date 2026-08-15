"""Context Engine — JARVIS always knows what you're doing right now.

Pure-Python Win32 (ctypes, no new deps). Collected on demand — the model
calls the get_context tool when the user says "this file", "what am I
looking at", "summarize this" etc. Nothing is collected in the background,
nothing leaves the machine except inside the AI request that needed it.

Provides: active window + app, visible windows, clipboard text (truncated),
battery/power state, local time.
"""
from __future__ import annotations

import ctypes
import time
from ctypes import wintypes

u32 = ctypes.windll.user32
k32 = ctypes.windll.kernel32

_CLIP_MAX = 400          # chars of clipboard shared with the model
_WIN_MAX  = 8            # how many open windows to list


def active_window() -> tuple[str, str]:
    """(window title, process exe name) of the foreground window."""
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return "", ""
    ln = u32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(ln + 1)
    u32.GetWindowTextW(hwnd, buf, ln + 1)
    pid = wintypes.DWORD()
    u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    name = ""
    h = k32.OpenProcess(0x1000, False, pid.value)   # QUERY_LIMITED_INFORMATION
    if h:
        nbuf = ctypes.create_unicode_buffer(260)
        sz = wintypes.DWORD(260)
        if k32.QueryFullProcessImageNameW(h, 0, nbuf, ctypes.byref(sz)):
            name = nbuf.value.rsplit("\\", 1)[-1]
        k32.CloseHandle(h)
    return buf.value, name


def open_windows(limit: int = _WIN_MAX) -> list[str]:
    """Titles of visible top-level windows (what's on screen)."""
    titles: list[str] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def _cb(hwnd, _):
        if len(titles) >= limit:
            return False
        if u32.IsWindowVisible(hwnd):
            ln = u32.GetWindowTextLengthW(hwnd)
            if ln > 0:
                b = ctypes.create_unicode_buffer(ln + 1)
                u32.GetWindowTextW(hwnd, b, ln + 1)
                if b.value.strip():
                    titles.append(b.value)
        return True

    u32.EnumWindows(_cb, 0)
    return titles


def clipboard_text(max_chars: int = _CLIP_MAX) -> str:
    CF_UNICODETEXT = 13
    if not u32.OpenClipboard(None):
        return ""
    try:
        h = u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = k32.GlobalLock(h)
        if not ptr:
            return ""
        try:
            return ctypes.wstring_at(ptr)[:max_chars]
        finally:
            k32.GlobalUnlock(h)
    except Exception:
        return ""
    finally:
        u32.CloseClipboard()


def battery() -> str:
    class _SPS(ctypes.Structure):
        _fields_ = [("ACLineStatus", ctypes.c_byte),
                    ("BatteryFlag", ctypes.c_byte),
                    ("BatteryLifePercent", ctypes.c_byte),
                    ("SystemStatusFlag", ctypes.c_byte),
                    ("BatteryLifeTime", wintypes.DWORD),
                    ("BatteryFullLifeTime", wintypes.DWORD)]
    s = _SPS()
    if not k32.GetSystemPowerStatus(ctypes.byref(s)):
        return "unknown"
    pct = s.BatteryLifePercent
    ac = "plugged in" if s.ACLineStatus == 1 else "on battery"
    return f"{pct}% ({ac})" if 0 <= pct <= 100 else ac


def get_context() -> dict:
    title, app = active_window()
    return {
        "time": time.strftime("%A %H:%M"),
        "active_app": app,
        "active_window": title,
        "open_windows": open_windows(),
        "clipboard": clipboard_text(),
        "battery": battery(),
    }


def format_context() -> str:
    """Compact one-blob string for the model."""
    c = get_context()
    lines = [f"Time: {c['time']}",
             f"Active app: {c['active_app']} — \"{c['active_window']}\"",
             f"Battery: {c['battery']}"]
    if c["open_windows"]:
        lines.append("Open windows: " + " | ".join(c["open_windows"]))
    if c["clipboard"]:
        lines.append(f"Clipboard (first {_CLIP_MAX} chars): {c['clipboard']}")
    return "\n".join(lines)


def demo():
    c = get_context()
    assert isinstance(c["open_windows"], list)
    assert c["time"]
    assert isinstance(c["clipboard"], str)      # may be empty, never crashes
    s = format_context()
    assert "Active app:" in s and "Time:" in s
    safe = f"context demo OK - {c['active_app']} | {c['active_window'][:40]}"
    print(safe.encode("ascii", "replace").decode())   # window titles may hold emoji


if __name__ == "__main__":
    demo()
