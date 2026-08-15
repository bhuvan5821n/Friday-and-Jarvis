"""Desktop-app runtime plumbing: logging, single instance, autostart.

Turns the F5-in-VS-Code workflow into a real Windows app lifecycle:
- rotating file logs in Logs/ (stdout/stderr captured when frozen, i.e. no console)
- single-instance guard on a localhost socket; a second launch tells the
  first instance to SHOW its window and exits
- HKCU Run-key autostart registration (no admin needed)
"""
from __future__ import annotations

import logging
import logging.handlers
import os
import socket
import sys
import threading
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent
LOG_DIR  = BASE_DIR / "Logs"
IPC_PORT = 48757   # jarvis single-instance / show-window channel (localhost only)


# ── logging ─────────────────────────────────────────────────────────────

class _StreamToLog:
    def __init__(self, log, level):
        self.log, self.level, self._buf = log, level, ""

    def write(self, text):
        self._buf += text
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            if line.strip():
                self.log.log(self.level, line)

    def flush(self):
        pass


def setup_logging(name: str = "jarvis") -> logging.Logger:
    LOG_DIR.mkdir(exist_ok=True)
    log = logging.getLogger(name)
    if log.handlers:
        return log
    log.setLevel(logging.INFO)
    h = logging.handlers.RotatingFileHandler(
        LOG_DIR / f"{name}.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    log.addHandler(h)

    # frozen exe has no console — capture prints and crashes into the log
    if getattr(sys, "frozen", False) or os.environ.get("JARVIS_LOG_STDIO"):
        sys.stdout = _StreamToLog(log, logging.INFO)
        sys.stderr = _StreamToLog(log, logging.ERROR)

    def _hook(exc_type, exc, tb):
        log.critical("UNCAUGHT", exc_info=(exc_type, exc, tb))
    sys.excepthook = _hook
    return log


def prefer_ipv4() -> None:
    """Reorder DNS results IPv4-first, process-wide.

    This machine's IPv6 routes to Google blackhole TCP (SYN timeout), and
    asyncio tries addresses strictly in getaddrinfo order — so the Gemini
    Live websocket burned its whole opening-handshake timeout on 8 dead
    IPv6 addresses while IPv4 connected in 0.1s. IPv6 still works as a
    fallback; it just stops going first."""
    orig = socket.getaddrinfo

    def _v4_first(*args, **kw):
        res = orig(*args, **kw)
        return sorted(res, key=lambda r: r[0] != socket.AF_INET)  # stable sort

    socket.getaddrinfo = _v4_first


# ── single instance ─────────────────────────────────────────────────────

def ensure_single_instance(on_show) -> bool:
    """True = we are the first instance (a listener thread now serves SHOW
    requests → on_show()). False = another instance runs; it was told to
    show itself and the caller should exit."""
    try:
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        # no SO_REUSEADDR: on Windows it would let a 2nd instance bind too
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        srv.bind(("127.0.0.1", IPC_PORT))
        srv.listen(2)
    except OSError:
        try:   # someone is already listening → ask them to show
            print("[IPC] Instance already running - sending SHOW")
            with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2) as c:
                c.sendall(b"SHOW")
            print("[IPC] SHOW sent")
        except Exception as e:
            print(f"[IPC] SHOW send FAILED: {e}")
        return False

    def _serve():
        while True:
            try:
                conn, _ = srv.accept()
                with conn:
                    if b"SHOW" in (conn.recv(16) or b""):
                        print("[IPC] SHOW received - invoking on_show")
                        on_show()
            except Exception as e:
                print(f"[IPC] server error: {e}")

    threading.Thread(target=_serve, daemon=True, name="JarvisIPC").start()
    return True


def request_show(timeout: float = 2.0) -> bool:
    """Ask a running instance to show its window. True if one answered."""
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=timeout) as c:
            c.sendall(b"SHOW")
        return True
    except Exception:
        return False


def instance_running() -> bool:
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=1):
            return True
    except Exception:
        return False


# ── Windows autostart (HKCU Run — per-user, no admin) ───────────────────

_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_RUN_VAL = "JarvisService"


def autostart_command() -> str:
    if getattr(sys, "frozen", False):
        return f'"{BASE_DIR / "JarvisService.exe"}"'
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py  = pyw if pyw.exists() else Path(sys.executable)
    return f'"{py}" "{BASE_DIR / "service.py"}"'


def install_autostart() -> str:
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, _RUN_VAL, 0, winreg.REG_SZ, autostart_command())
    return f"Autostart installed: {autostart_command()}"


def uninstall_autostart() -> str:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as k:
            winreg.DeleteValue(k, _RUN_VAL)
        return "Autostart removed."
    except FileNotFoundError:
        return "Autostart was not installed."


def autostart_installed() -> bool:
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _RUN_KEY) as k:
            winreg.QueryValueEx(k, _RUN_VAL)
        return True
    except FileNotFoundError:
        return False


def demo():
    log = setup_logging("runtime_demo")
    log.info("logging OK")
    assert (LOG_DIR / "runtime_demo.log").exists()

    shown = []
    first = ensure_single_instance(lambda: shown.append(1))
    assert first, "should be first instance"
    second = ensure_single_instance(lambda: None)
    assert not second, "second instance must be refused"
    import time
    time.sleep(0.3)
    assert shown, "SHOW request did not reach first instance"
    assert instance_running()
    print("runtime demo OK  (autostart cmd: " + autostart_command() + ")")


if __name__ == "__main__":
    demo()
