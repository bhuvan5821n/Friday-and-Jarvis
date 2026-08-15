"""Complete shutdown of everything this project owns — and nothing else.

Two stages.  Stage 1 asks each process to stop through IPC and waits for it to
acknowledge, so databases commit, logs flush and the microphone is released
cleanly.  Stage 2 force-terminates only what is left *and* only after
:func:`process_registry.verify` confirms the PID is still ours.

The watchdog is stopped first and deliberately.  It exists to relaunch the
assistant whenever it dies, so shutting the assistant down while the watchdog
lives would simply resurrect it. Its autostart registration is left in place,
so crash protection returns at next login.

Never terminates by process name. ``taskkill /im python.exe`` would kill the
user's unrelated scripts, so it is not used anywhere in this file.
"""
from __future__ import annotations

import logging
import os
import socket
import time

import psutil

from . import process_registry as registry
from .protocol import IPC_PORT, SPEECHCORE_PORT, get_token, encode

log = logging.getLogger("lifecycle.shutdown")

#: Stop order. The watchdog goes first so nothing restarts what follows; the
#: assistant next so it can flush and release devices; SpeechCore last because
#: the assistant talks to it while shutting down.
_STOP_ORDER = ("watchdog", "mic_overlay", "assistant", "speechcore", "helper")

#: Ports this project may listen on. Used only for post-shutdown verification.
_PROJECT_PORTS = (IPC_PORT, SPEECHCORE_PORT)


def _ask_assistant_to_stop(timeout: float) -> bool:
    """Send SHUTDOWN over the assistant's channel and wait for its reply."""
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=2.0) as c:
            c.sendall(encode("SHUTDOWN", get_token()))
            c.settimeout(timeout)
            try:
                reply = c.recv(256)
            except socket.timeout:
                reply = b""
        log.info("assistant acknowledged shutdown: %s",
                 (reply or b"<no reply>").decode("utf-8", "replace").strip())
        return True
    except Exception as exc:
        log.info("no assistant answered on %s (%s)", IPC_PORT, exc)
        return False


def _ask_speechcore_to_stop() -> bool:
    """SpeechCore speaks its own line protocol; ask it to stop and release."""
    for command in (b"STOP\n", b"SHUTDOWN\n"):
        try:
            with socket.create_connection(("127.0.0.1", SPEECHCORE_PORT),
                                          timeout=1.5) as c:
                c.sendall(command)
            log.info("sent %s to SpeechCore", command.strip().decode())
            return True
        except Exception:
            continue
    return False


def _terminate(proc: psutil.Process, role: str, grace: float = 4.0) -> bool:
    """Terminate one verified project process, then its verified children.

    Children are collected *before* terminating the parent, because once the
    parent exits the tree is unwalkable and orphans would survive.
    """
    try:
        children = proc.children(recursive=True)
    except Exception:
        children = []

    for target in (*children, proc):
        try:
            target.terminate()
        except psutil.NoSuchProcess:
            continue
        except Exception as exc:
            log.warning("terminate failed for pid %s: %s", target.pid, exc)

    gone, alive = psutil.wait_procs([*children, proc], timeout=grace)
    for target in alive:
        try:
            log.warning("pid %s ignored terminate; killing", target.pid)
            target.kill()
        except psutil.NoSuchProcess:
            pass
        except Exception as exc:
            log.warning("kill failed for pid %s: %s", target.pid, exc)
    psutil.wait_procs(alive, timeout=2.0)

    still = [t for t in (*children, proc) if t.is_running()]
    if still:
        log.error("%s: %d process(es) survived", role, len(still))
        return False
    log.info("%s: stopped (%d process(es))", role, len(gone) + len(alive))
    return True


def _port_free(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.6):
            return False
    except Exception:
        return True


def verify_clean() -> dict:
    """Post-shutdown proof: no registered process, no project port listening."""
    remaining = [
        {"role": rec.role, "pid": rec.pid}
        for rec, _ in registry.live_records()
    ]
    untracked = []
    for proc in registry.discover_untracked():
        try:
            if proc.pid == os.getpid():
                continue
            untracked.append({"pid": proc.pid, "exe": proc.info.get("exe") or ""})
        except Exception:
            continue
    busy = [p for p in _PROJECT_PORTS if not _port_free(p)]
    return {"registered_remaining": remaining, "untracked_remaining": untracked,
            "ports_listening": busy,
            "clean": not remaining and not untracked and not busy}


def shutdown_all(timeout: float = 8.0) -> dict:
    """Stop every project process. Returns a report of what happened."""
    started = time.time()
    report: dict = {"graceful": [], "forced": [], "errors": []}
    log.info("=== SHUTDOWN_ALL requested ===")

    # Stage 0 — stop the watchdog before anything it would restart.
    for record, proc in registry.live_records(("watchdog",)):
        if _terminate(proc, "watchdog"):
            report["forced"].append({"role": "watchdog", "pid": record.pid})
            registry.unregister(record.pid)
        else:
            report["errors"].append(f"watchdog pid {record.pid} survived")

    # Stage 1 — graceful, in dependency order.
    if _ask_assistant_to_stop(timeout=min(timeout, 6.0)):
        report["graceful"].append("assistant")
    if _ask_speechcore_to_stop():
        report["graceful"].append("speechcore")

    deadline = started + timeout
    while time.time() < deadline:
        if not registry.live_records(("assistant", "speechcore", "mic_overlay")):
            break
        time.sleep(0.35)

    # Stage 2 — force only what remains, and only if it verifies as ours.
    for role in _STOP_ORDER:
        for record, proc in registry.live_records((role,)):
            log.warning("%s pid %s did not exit gracefully; forcing",
                        role, record.pid)
            if _terminate(proc, role):
                report["forced"].append({"role": role, "pid": record.pid})
                registry.unregister(record.pid)
            else:
                report["errors"].append(f"{role} pid {record.pid} survived")

    # Stage 3 — sweep processes no registry row knows about (crash leftovers,
    # or builds that predate the registry). The watchdog must die first or it
    # respawns the assistant mid-sweep; and one pass is not enough, because a
    # respawn that slipped in between listing and killing would survive — so
    # repeat until a sweep finds nothing.
    def _watchdogs_first(proc: psutil.Process) -> int:
        try:
            argv = " ".join(proc.info.get("cmdline") or []).lower()
        except Exception:
            argv = ""
        return 0 if "service.py" in argv or "jarvisservice" in argv else 1

    for sweep in range(4):
        leftovers = [p for p in registry.discover_untracked()
                     if p.pid != os.getpid()]
        if not leftovers:
            break
        leftovers.sort(key=_watchdogs_first)
        for proc in leftovers:
            try:
                exe = proc.info.get("exe") or ""
                log.warning("untracked project process pid %s (%s); stopping",
                            proc.pid, exe)
                if _terminate(proc, "untracked"):
                    report["forced"].append({"role": "untracked", "pid": proc.pid})
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
            except Exception as exc:
                report["errors"].append(f"untracked pid {proc.pid}: {exc}")
        time.sleep(0.6)  # let any in-flight respawn appear, then re-sweep

    registry.clear()
    report["verification"] = verify_clean()
    report["seconds"] = round(time.time() - started, 2)
    log.info("=== SHUTDOWN_ALL finished in %.2fs clean=%s ===",
             report["seconds"], report["verification"]["clean"])
    return report


def demo() -> None:
    """Self-check that touches no real process."""
    assert "watchdog" == _STOP_ORDER[0], "watchdog must stop first"
    assert _port_free(59999) is True
    v = verify_clean()
    assert set(v) >= {"registered_remaining", "ports_listening", "clean"}
    print("shutdown_manager demo OK; clean =", v["clean"])


if __name__ == "__main__":
    demo()
