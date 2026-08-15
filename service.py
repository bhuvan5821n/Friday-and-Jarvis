"""JarvisService — background watchdog. No UI, near-zero CPU.

Starts with Windows (HKCU Run key), launches Jarvis hidden to the tray,
and restarts it if it ever dies. Crash-looping is rate-limited with
exponential backoff so a broken install can't spin the CPU.

Commands:
    python service.py               run the watchdog
    python service.py install       register autostart (per-user, no admin)
    python service.py uninstall     remove autostart
    python service.py status        show autostart + running state
"""
from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent
sys.path.insert(0, str(BASE_DIR))

from core.noconsole import install as _install_noconsole   # noqa: E402
_install_noconsole()

from core.runtime import (   # noqa: E402
    autostart_installed, ensure_single_instance, install_autostart,
    instance_running, setup_logging, uninstall_autostart,
)

CHECK_INTERVAL = 10   # seconds between liveness checks — near-zero CPU


def _jarvis_cmd() -> list[str]:
    if getattr(sys, "frozen", False):
        return [str(BASE_DIR / "Jarvis.exe"), "--hidden"]
    pyw = Path(sys.executable).with_name("pythonw.exe")
    py  = str(pyw if pyw.exists() else sys.executable)
    return [py, str(BASE_DIR / "main.py"), "--hidden"]


def _launch(log) -> None:
    cmd = _jarvis_cmd()
    log.info(f"Launching: {' '.join(cmd)}")
    subprocess.Popen(cmd, cwd=str(BASE_DIR),
                     creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def watchdog() -> None:
    log = setup_logging("service")
    log.info("JarvisService starting")
    try:
        # Visible to the lifecycle launcher, so Ctrl+Alt+Shift+X can stop the
        # watchdog *first* — otherwise it would resurrect what was shut down.
        from launcher import process_registry
        process_registry.register("watchdog")
    except Exception as e:
        log.warning(f"process registry unavailable: {e}")
    backoff = 3
    while True:
        if instance_running():
            backoff = 3          # healthy → reset the crash backoff
        else:
            _launch(log)
            time.sleep(backoff)  # give it time to boot before re-checking
            if not instance_running():
                backoff = min(backoff * 2, 300)
                log.warning(f"Jarvis not up yet; next retry in {backoff}s")
        time.sleep(CHECK_INTERVAL)


def main() -> None:
    setup_logging("service")   # frozen --noconsole exe: print() → log file
    cmd = (sys.argv[1] if len(sys.argv) > 1 else "").lower()
    if cmd == "install":
        print(install_autostart())
    elif cmd == "uninstall":
        print(uninstall_autostart())
    elif cmd == "status":
        print(f"autostart: {'installed' if autostart_installed() else 'not installed'}")
        print(f"jarvis:    {'running' if instance_running() else 'not running'}")
    else:
        watchdog()


def demo():
    """Self-check without launching anything real."""
    cmd = _jarvis_cmd()
    assert cmd[-1] == "--hidden" and "main.py" in cmd[-2] or "Jarvis.exe" in cmd[0]
    assert isinstance(instance_running(), bool)
    assert CHECK_INTERVAL >= 5, "watchdog must stay near-zero CPU"
    print("service demo OK:", " ".join(cmd))


if __name__ == "__main__":
    if "--demo" in sys.argv:
        demo()
    else:
        main()
