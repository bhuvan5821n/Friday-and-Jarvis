"""Locate and safely run Agent Reach and its venv tools.

Agent Reach's own design (core.py): the CLI installs and health-checks;
agents then call upstream tools directly. This module is the only place that
knows where those executables live. Everything runs with argument arrays (no
shell), timeouts, hidden windows, separate stdout/stderr, and cancellation.
"""
from __future__ import annotations

import json
import logging
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("webintel.agent_reach")

#: Default install root; overridable via config so a moved install still works.
DEFAULT_ROOT = Path(r"D:\AI-Tools\Agent-Reach")

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass
class AgentReachInstall:
    root: Path
    exe: Path                 # agent-reach.exe
    python: Path              # venv python
    yt_dlp: Path | None       # venv yt-dlp.exe, if present
    version: str = ""

    @property
    def valid(self) -> bool:
        return self.exe.exists() and self.python.exists()


def locate(root: Path | str | None = None) -> AgentReachInstall | None:
    """Find the installation; None (never a guess) when it isn't there."""
    root = Path(root) if root else DEFAULT_ROOT
    scripts = root / ".venv" / "Scripts"
    install = AgentReachInstall(
        root=root,
        exe=scripts / "agent-reach.exe",
        python=scripts / "python.exe",
        yt_dlp=(scripts / "yt-dlp.exe") if (scripts / "yt-dlp.exe").exists() else None,
    )
    if not install.valid:
        log.warning("Agent Reach not found at %s", root)
        return None
    return install


class CommandRunner:
    """Run one external tool invocation safely and trackably.

    - argument arrays only; nothing user-controlled is ever a shell string
    - hard timeout, hidden window, UTF-8 with replacement
    - every live Popen is registered so cancel_all()/shutdown can stop it
    """

    def __init__(self):
        self._procs: set[subprocess.Popen] = set()
        self._lock = threading.Lock()
        self._cancelled = False

    def run(self, argv: list[str | Path], timeout: float = 30.0,
            cwd: Path | None = None) -> tuple[int, str, str]:
        """Returns (returncode, stdout, stderr). Raises on timeout."""
        if self._cancelled:
            raise RuntimeError("runner is cancelled")
        argv = [str(a) for a in argv]
        log.info("run: %s (timeout=%ss)", " ".join(argv[:3]) + " …", timeout)
        proc = subprocess.Popen(
            argv, cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
            creationflags=_NO_WINDOW)
        with self._lock:
            self._procs.add(proc)
        try:
            out, err = proc.communicate(timeout=timeout)
            return proc.returncode, out or "", err or ""
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate(timeout=5)
            raise TimeoutError(f"{argv[0]} timed out after {timeout}s")
        finally:
            with self._lock:
                self._procs.discard(proc)

    def cancel_all(self) -> int:
        """Kill every live child. Returns how many were stopped."""
        with self._lock:
            procs, self._cancelled = list(self._procs), True
        stopped = 0
        for proc in procs:
            try:
                if proc.poll() is None:
                    proc.kill()
                    proc.wait(timeout=5)   # reap so live_count is truthful
                    stopped += 1
            except Exception as exc:
                log.warning("cancel failed for pid %s: %s", proc.pid, exc)
        self._cancelled = False
        return stopped

    @property
    def live_count(self) -> int:
        with self._lock:
            return sum(1 for p in self._procs if p.poll() is None)


def doctor(install: AgentReachInstall, runner: CommandRunner | None = None,
           timeout: float = 60.0) -> dict:
    """Parsed `agent-reach doctor --json` — the single source of capability
    truth. Empty dict (not fabricated health) when the check fails."""
    runner = runner or CommandRunner()
    try:
        rc, out, err = runner.run([install.exe, "doctor", "--json"],
                                  timeout=timeout)
        if rc != 0:
            log.warning("doctor rc=%s: %s", rc, err[:200])
            return {}
        return json.loads(out)
    except Exception as exc:
        log.warning("doctor failed: %s", exc)
        return {}
