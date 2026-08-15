"""assistant_launcher — the tiny process behind every global shortcut.

A Start Menu shortcut fires this, it does one job, then it exits.  Nothing
here stays resident, so after a full shutdown no project code is running and
the shortcuts still work — Windows itself relaunches this on the next press.

Commands:
    --open jarvis | --open friday    open/restore the assistant, set persona
    --mic-open                       compact mic overlay + unmute + listen
    --mic-close                      mute, release mic, hide overlay
    --shutdown-all                   stop every project process
    --install-shortcuts              create the Start Menu hotkeys
    --remove-shortcuts               remove only our shortcuts
    --repair-shortcuts               remove + recreate
    --status                         print lifecycle status (for diagnosis)
"""
from __future__ import annotations

import argparse
import logging
import logging.handlers
import os
import subprocess
import sys
import time
from pathlib import Path

# Running frozen, our modules sit next to the exe; running from source, the
# repo root is one level up.
if getattr(sys, "frozen", False):
    sys.path.insert(0, str(Path(sys.executable).parent))
else:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from launcher.protocol import (BASE_DIR, LOG_DIR, get_token, instance_running,
                               load_config, save_config, send)
from launcher import process_registry as registry

log = logging.getLogger("lifecycle")


def _setup_logging() -> None:
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "lifecycle.log", maxBytes=512_000, backupCount=3,
        encoding="utf-8")
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s"))
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    root.addHandler(handler)


def _notify(title: str, message: str) -> None:
    """Small toast so failures are visible without any console window."""
    try:
        script = (
            "[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null;"
            "$t=[Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02);"
            f"$t.GetElementsByTagName('text')[0].AppendChild($t.CreateTextNode('{title}')) > $null;"
            f"$t.GetElementsByTagName('text')[1].AppendChild($t.CreateTextNode('{message}')) > $null;"
            "$n=[Windows.UI.Notifications.ToastNotification]::new($t);"
            "[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('FRIDAY Assistant').Show($n)")
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=10,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except Exception:
        # Fall back to a message box; never a console.
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, message, title, 0x40)
        except Exception:
            pass


def _assistant_cmd(extra: list[str]) -> list[str]:
    """How to start the assistant without any console window."""
    frozen = BASE_DIR / "Jarvis.exe"
    if frozen.exists():
        return [str(frozen), *extra]
    pyw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = Path(sys.executable).with_name("pythonw.exe")
    py = str(pyw if pyw.exists() else sys.executable)
    return [py, str(BASE_DIR / "main.py"), *extra]


def _cold_start(persona: str | None, hidden: bool = False) -> bool:
    """Launch the assistant and wait until its IPC channel answers READY."""
    cfg = load_config()
    extra = ["--hidden"] if hidden else []
    if persona:
        extra += ["--persona", persona]
    cmd = _assistant_cmd(extra)
    log.info("cold start: %s", " ".join(cmd))
    try:
        proc = subprocess.Popen(
            cmd, cwd=str(BASE_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except FileNotFoundError as exc:
        log.error("assistant executable missing: %s", exc)
        _notify("Assistant failed to start", f"Missing executable: {exc}")
        return False
    registry.register("assistant", proc.pid)

    deadline = time.time() + float(cfg.get("startup_timeout_seconds", 20))
    while time.time() < deadline:
        reply = send("PING", timeout=1.0)
        if reply is not None:
            state = reply.get("state", "READY")
            log.info("assistant answered PING, state=%s", state)
            if state in ("READY", "STARTING", "INITIALIZING_BACKEND",
                         "LOADING_MODELS", "CONNECTING_SERVICES"):
                return True
            if state == "FAILED":
                _notify("Assistant failed to start", "Backend reported FAILED.")
                return False
        if proc.poll() is not None:
            log.error("assistant exited during startup (rc=%s)", proc.returncode)
            _notify("Assistant failed to start",
                    f"Process exited with code {proc.returncode}. See Logs/.")
            return False
        time.sleep(0.4)
    log.error("assistant did not become ready in time")
    _notify("Assistant is slow to start",
            "Startup timed out; it may still be loading. Try again shortly.")
    return False


# ---- shortcut actions ------------------------------------------------------

def action_open(persona: str) -> int:
    """Ctrl+Alt+J / Ctrl+Alt+F: open or restore, switch persona, focus."""
    log.info("action: open %s", persona)
    cfg = load_config()
    if cfg.get("remember_last_assistant", True):
        cfg["default_assistant"] = persona
        save_config(cfg)

    if instance_running():
        for command in ("SET_PERSONA", "SHOW_WINDOW", "RESTORE_WINDOW",
                        "FOCUS_INPUT"):
            reply = send(command, persona=persona)
            if reply is None:
                log.warning("%s: no reply; instance may be a legacy build",
                            command)
        log.info("existing instance restored as %s", persona)
        return 0
    return 0 if _cold_start(persona) else 1


def action_mic_open() -> int:
    """Ctrl+Alt+M: overlay + unmute + listen, without the full dashboard."""
    log.info("action: mic-open")
    cfg = load_config()
    persona = cfg.get("default_assistant", "friday")

    if not instance_running():
        # Minimal start: hidden window, no dashboard on screen.
        if not _cold_start(persona, hidden=True):
            return 1
    reply = send("OPEN_MIC", persona=persona, timeout=5.0)
    if reply is None:
        _notify("Microphone", "The assistant did not answer. See Logs/lifecycle.log.")
        return 1
    if reply.get("error"):
        _notify("Microphone unavailable", str(reply["error"]))
        return 1
    return 0


def action_mic_close() -> int:
    """Ctrl+Alt+Shift+M: mute + release the device. Assistant keeps running."""
    log.info("action: mic-close")
    if not instance_running():
        log.info("nothing running; mic-close is a no-op")
        return 0
    reply = send("CLOSE_MIC", timeout=5.0)
    if reply is None:
        _notify("Microphone", "The assistant did not answer the mute request.")
        return 1
    return 0


def action_shutdown_all() -> int:
    """Ctrl+Alt+Shift+X: stop everything project-owned, verify, report."""
    log.info("action: shutdown-all")
    from launcher.shutdown_manager import shutdown_all
    cfg = load_config()
    report = shutdown_all(timeout=float(cfg.get("shutdown_timeout_seconds", 8)))
    v = report["verification"]
    if v["clean"]:
        log.info("shutdown verified clean")
        _notify("Assistant", "JARVIS and FRIDAY are fully shut down.")
        return 0
    log.error("shutdown NOT clean: %s", v)
    _notify("Shutdown incomplete",
            f"{len(v['registered_remaining']) + len(v['untracked_remaining'])} "
            "process(es) remain. See Logs/lifecycle.log.")
    return 1


def action_status() -> int:
    import json
    from launcher.shutdown_manager import verify_clean
    info = {
        "instance_running": instance_running(),
        "ping": send("PING"),
        "registry": [r.to_dict() for r, _ in registry.live_records()],
        "clean_if_shutdown": verify_clean(),
    }
    print(json.dumps(info, indent=2))
    return 0


def main(argv: list[str] | None = None) -> int:
    _setup_logging()
    parser = argparse.ArgumentParser(prog="assistant_launcher")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--open", choices=("jarvis", "friday"))
    group.add_argument("--mic-open", action="store_true")
    group.add_argument("--mic-close", action="store_true")
    group.add_argument("--shutdown-all", action="store_true")
    group.add_argument("--install-shortcuts", action="store_true")
    group.add_argument("--remove-shortcuts", action="store_true")
    group.add_argument("--repair-shortcuts", action="store_true")
    group.add_argument("--status", action="store_true")
    args = parser.parse_args(argv)

    get_token()  # ensure the per-install token exists before anything talks

    try:
        if args.open:
            return action_open(args.open)
        if args.mic_open:
            return action_mic_open()
        if args.mic_close:
            return action_mic_close()
        if args.shutdown_all:
            return action_shutdown_all()
        if args.status:
            return action_status()
        from launcher.shortcut_installer import install, remove, repair
        if args.install_shortcuts:
            return install()
        if args.remove_shortcuts:
            return remove()
        if args.repair_shortcuts:
            return repair()
    except Exception as exc:  # noqa: BLE001 — a shortcut must never die silently
        log.exception("launcher action failed")
        _notify("Assistant launcher error", f"{type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
