"""No flashing consoles — process-wide root-cause fix.

Jarvis.exe is a windowed (no-console) app. On Windows, every console-
subsystem child (powershell, cmd, nvidia-smi, schtasks, tasklist, ...)
spawned by a windowed parent gets a brand-new visible console window.
The project has ~100 subprocess call sites across actions/, ui.py, agent/;
patching each one is unmaintainable — the next call site would flash again.

So: patch subprocess.Popen once, here. Every child in this process gets
CREATE_NO_WINDOW unless the caller explicitly asked for console control
(CREATE_NEW_CONSOLE / DETACHED_PROCESS). GUI apps are unaffected — the
flag only suppresses console allocation. Deliberate terminal opens still
work: `start cmd` launches a fresh console of its own regardless.
"""
from __future__ import annotations

import subprocess
import sys


def install() -> None:
    if sys.platform != "win32":
        return
    if getattr(subprocess.Popen.__init__, "_noconsole", False):
        return   # already installed

    no_window = subprocess.CREATE_NO_WINDOW
    explicit  = (subprocess.CREATE_NEW_CONSOLE
                 | no_window
                 | getattr(subprocess, "DETACHED_PROCESS", 0x00000008))
    orig = subprocess.Popen.__init__

    def patched(self, *args, **kwargs):
        flags = kwargs.get("creationflags", 0)
        if not (flags & explicit):
            kwargs["creationflags"] = flags | no_window
        orig(self, *args, **kwargs)

    patched._noconsole = True
    subprocess.Popen.__init__ = patched


def demo():
    install()
    install()   # idempotent
    assert getattr(subprocess.Popen.__init__, "_noconsole", False) or sys.platform != "win32"
    r = subprocess.run(["cmd", "/c", "echo hidden-ok"],
                       capture_output=True, text=True) if sys.platform == "win32" else None
    if r is not None:
        assert "hidden-ok" in r.stdout
    print("noconsole demo OK")


if __name__ == "__main__":
    demo()
