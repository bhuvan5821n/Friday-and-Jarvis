"""Registry of processes this project owns.

The only thing standing between "force-terminate a stale PID" and "kill a
stranger's program" is proof that the PID is ours.  Windows recycles PIDs, so a
PID alone is never proof.  Every record therefore carries the process creation
time and an executable path, and :func:`verify` re-checks both before any
caller is allowed to terminate anything.

Contains no secrets: roles, PIDs, paths and timestamps only.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import sys
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import psutil

from .protocol import REGISTRY_PATH, _atomic_write

log = logging.getLogger("lifecycle.registry")

#: Roles this project may run. Used to order a graceful shutdown and to reject
#: nonsense entries.
ROLES = ("assistant", "watchdog", "speechcore", "mic_overlay", "helper")

#: Creation times drift by sub-second amounts between readings; treat records
#: within this window as the same process.
_CREATE_TIME_TOLERANCE = 2.0


@dataclass
class ProcessRecord:
    role: str
    pid: int
    parent_pid: int = 0
    executable: str = ""
    started_at: str = ""
    create_time: float = 0.0
    launch_token: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Registry:
    session_id: str = ""
    processes: list = field(default_factory=list)


def _read() -> Registry:
    try:
        raw = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        reg = Registry(session_id=str(raw.get("session_id", "")))
        for entry in raw.get("processes", []):
            try:
                if entry.get("role") not in ROLES:
                    continue
                reg.processes.append(ProcessRecord(
                    role=str(entry["role"]),
                    pid=int(entry["pid"]),
                    parent_pid=int(entry.get("parent_pid", 0)),
                    executable=str(entry.get("executable", "")),
                    started_at=str(entry.get("started_at", "")),
                    create_time=float(entry.get("create_time", 0.0)),
                    launch_token=str(entry.get("launch_token", "")),
                ))
            except Exception:
                continue  # skip a corrupt row, keep the rest
        return reg
    except FileNotFoundError:
        return Registry()
    except Exception as exc:
        log.warning("registry unreadable (%s); starting a fresh one", exc)
        return Registry()


def _write(reg: Registry) -> None:
    _atomic_write(REGISTRY_PATH, json.dumps(
        {"session_id": reg.session_id,
         "processes": [p.to_dict() for p in reg.processes]}, indent=2))


def register(role: str, pid: int | None = None, executable: str = "") -> ProcessRecord | None:
    """Record a process as project-owned. Returns the stored record."""
    if role not in ROLES:
        raise ValueError(f"unknown role {role!r}")
    pid = int(pid if pid is not None else os.getpid())
    try:
        proc = psutil.Process(pid)
        create_time = proc.create_time()
        exe = executable or (proc.exe() or "")
        parent = proc.ppid()
    except Exception as exc:
        log.warning("cannot register pid %s: %s", pid, exc)
        return None

    reg = _read()
    if not reg.session_id:
        reg.session_id = secrets.token_hex(8)
    # One record per (role, pid); re-registering refreshes it.
    reg.processes = [p for p in reg.processes if p.pid != pid]
    record = ProcessRecord(
        role=role, pid=pid, parent_pid=parent, executable=exe,
        started_at=time.strftime("%Y-%m-%dT%H:%M:%S"),
        create_time=create_time, launch_token=secrets.token_hex(8),
    )
    reg.processes.append(record)
    _write(reg)
    log.info("registered %s pid=%s", role, pid)
    return record


def unregister(pid: int | None = None) -> None:
    pid = int(pid if pid is not None else os.getpid())
    reg = _read()
    before = len(reg.processes)
    reg.processes = [p for p in reg.processes if p.pid != pid]
    if len(reg.processes) != before:
        _write(reg)
        log.info("unregistered pid=%s", pid)


def verify(record: ProcessRecord) -> psutil.Process | None:
    """Return the live process only if it is still the one we registered.

    Guards against PID reuse: a matching PID whose creation time or executable
    differs is a *different* program that happens to have inherited the number,
    and must never be terminated.
    """
    try:
        proc = psutil.Process(record.pid)
    except psutil.NoSuchProcess:
        return None
    except Exception:
        return None

    try:
        if record.create_time and abs(proc.create_time() - record.create_time) > _CREATE_TIME_TOLERANCE:
            log.warning("pid %s was recycled (create_time differs) — not ours",
                        record.pid)
            return None
    except Exception:
        return None

    if record.executable:
        try:
            live = (proc.exe() or "").lower()
            if live and live != record.executable.lower():
                log.warning("pid %s executable changed — not ours", record.pid)
                return None
        except psutil.AccessDenied:
            pass  # cannot read the path; creation time already matched
        except Exception:
            return None
    return proc


def live_records(roles: tuple[str, ...] | None = None) -> list[tuple[ProcessRecord, psutil.Process]]:
    """Verified, still-running records. Stale rows are pruned as a side effect."""
    reg = _read()
    alive, keep = [], []
    for record in reg.processes:
        if roles and record.role not in roles:
            keep.append(record)
            continue
        proc = verify(record)
        if proc is not None:
            keep.append(record)
            alive.append((record, proc))
    if len(keep) != len(reg.processes):
        reg.processes = keep
        _write(reg)
    return alive


def prune() -> int:
    """Drop records whose processes are gone. Returns how many were removed."""
    reg = _read()
    keep = [p for p in reg.processes if verify(p) is not None]
    removed = len(reg.processes) - len(keep)
    if removed:
        reg.processes = keep
        _write(reg)
    return removed


def clear() -> None:
    """Remove the registry file entirely — used after a verified shutdown."""
    try:
        REGISTRY_PATH.unlink()
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("could not remove registry: %s", exc)


#: Executables that may legitimately mention this repo in their command line
#: without belonging to us — an editor or shell opened *on* the project folder.
#: Matching the repo path in argv is not ownership; running our code is.
_NEVER_OURS = frozenset({
    "code.exe", "code - insiders.exe", "devenv.exe", "pycharm64.exe",
    "sublime_text.exe", "notepad++.exe", "windowsterminal.exe",
    "wt.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
    "explorer.exe", "git.exe", "bash.exe", "sh.exe", "node.exe",
    "msedge.exe", "chrome.exe", "firefox.exe", "python.exe", "pythonw.exe",
})

#: Scripts that are this project's own entry points. A source-run interpreter
#: is ours only when it is executing one of these.
_OUR_SCRIPTS = ("main.py", "service.py", "assistant_launcher.py",
                "mic_overlay.py", "ui.py")


def _self_and_ancestors() -> set[int]:
    """PIDs that must never be treated as targets: us and our parent chain.

    The launcher itself runs from the project venv, so without this it would
    flag (or worse, terminate) its own process tree during a sweep.
    """
    pids = {os.getpid()}
    try:
        proc = psutil.Process()
        for parent in proc.parents():
            pids.add(parent.pid)
    except Exception:
        pass
    return pids


def discover_untracked() -> list[psutil.Process]:
    """Find project processes missing from the registry.

    A crash, or a build started before the launcher existed, leaves processes
    nothing has a record of. Ownership is proved two ways only:

    * the executable itself lives inside the repo (SpeechCore.exe, a frozen
      Jarvis.exe, or the project's own venv interpreter), or
    * a project entry-point script appears in the command line.

    Merely mentioning the repo path is *not* proof — VS Code opened on this
    folder has the path in its argv, and terminating the user's editor is
    exactly the failure this guard exists to prevent.
    """
    root = str(Path(REGISTRY_PATH).parent.parent).lower().rstrip("\\/")
    known = {p.pid for p, _ in live_records()}
    excluded = _self_and_ancestors()
    found = []
    for proc in psutil.process_iter(["pid", "exe", "cmdline", "name"]):
        try:
            if proc.pid in known or proc.pid in excluded:
                continue
            name = (proc.info.get("name") or "").lower()
            exe = (proc.info.get("exe") or "").lower()
            inside_repo = exe.startswith(root + os.sep) or exe.startswith(root + "/")

            # An interpreter living in our own venv still has a generic name,
            # so judge it by location first.
            if inside_repo and name not in {"code.exe", "node.exe"}:
                found.append(proc)
                continue
            if name in _NEVER_OURS and not inside_repo:
                continue

            argv = proc.info.get("cmdline") or []
            script = next((str(a) for a in argv[1:]
                           if str(a).lower().endswith(".py")), "")
            if script:
                s = script.lower()
                if s.startswith(root) and Path(s).name in _OUR_SCRIPTS:
                    found.append(proc)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        except Exception:
            continue
    return found


def demo() -> None:
    """Self-check: register, verify, reject a recycled PID, unregister."""
    rec = register("helper")
    assert rec is not None and rec.pid == os.getpid()
    assert verify(rec) is not None, "our own process must verify"

    # A record whose creation time disagrees must be rejected.
    impostor = ProcessRecord(role="helper", pid=os.getpid(),
                             executable=sys.executable,
                             create_time=rec.create_time - 9999)
    assert verify(impostor) is None, "recycled PID must not verify"

    assert any(p.pid == os.getpid() for p, _ in live_records())
    unregister()
    assert not any(p.pid == os.getpid() for p, _ in live_records())
    print("process_registry demo OK")


if __name__ == "__main__":
    demo()
