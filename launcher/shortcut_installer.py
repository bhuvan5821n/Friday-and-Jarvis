"""Start Menu shortcut installer — the piece that makes the hotkeys global.

Windows Explorer itself watches Start Menu ``.lnk`` hotkeys, so a key press
launches the launcher even when zero project processes are running — exactly
the property a resident Python hotkey listener could never have.

Shortcuts live in:
    %APPDATA%/Microsoft/Windows/Start Menu/Programs/JARVIS FRIDAY/

Never touches the desktop and never deletes anything outside that folder.
"""
from __future__ import annotations

import logging
import os
import subprocess
import sys
from pathlib import Path

from .protocol import BASE_DIR, load_config

log = logging.getLogger("lifecycle.shortcuts")

FOLDER = (Path(os.environ.get("APPDATA", "")) /
          "Microsoft" / "Windows" / "Start Menu" / "Programs" / "JARVIS FRIDAY")

#: name -> (argument, description, config key for the hotkey)
SHORTCUTS = {
    "Open JARVIS": ("--open jarvis", "Open or restore the JARVIS interface", "jarvis"),
    "Open FRIDAY": ("--open friday", "Open or restore the FRIDAY interface", "friday"),
    "Open AI Microphone": ("--mic-open", "Compact microphone: unmute and listen", "mic_open"),
    "Close AI Microphone": ("--mic-close", "Mute and release the microphone", "mic_close"),
    "Shut Down AI Completely": ("--shutdown-all", "Stop every JARVIS/FRIDAY process", "shutdown_all"),
}


def _launcher_target() -> tuple[str, str]:
    """(target, arguments-prefix) for the .lnk files.

    Prefers the frozen assistant_launcher.exe; falls back to pythonw.exe so
    the shortcuts work before packaging without ever flashing a console.
    """
    for frozen in (BASE_DIR / "assistant_launcher.exe",
                   BASE_DIR / "dist" / "launcher" / "assistant_launcher.exe"):
        if frozen.exists():
            return str(frozen), ""
    pyw = BASE_DIR / ".venv" / "Scripts" / "pythonw.exe"
    if not pyw.exists():
        pyw = Path(sys.executable).with_name("pythonw.exe")
    script = BASE_DIR / "launcher" / "assistant_launcher.py"
    return str(pyw), f'"{script}" '


def _ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        capture_output=True, text=True, timeout=30,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _create_one(name: str, args: str, description: str, hotkey: str) -> bool:
    """Create/overwrite one .lnk with its hotkey via the WScript.Shell COM API."""
    target, prefix = _launcher_target()
    lnk = FOLDER / f"{name}.lnk"
    script = f"""
$sh = New-Object -ComObject WScript.Shell
$s = $sh.CreateShortcut('{lnk}')
$s.TargetPath = '{target}'
$s.Arguments = '{prefix}{args}'
$s.WorkingDirectory = '{BASE_DIR}'
$s.Description = '{description}'
$s.Hotkey = '{hotkey}'
$s.WindowStyle = 7
$s.Save()
"""
    result = _ps(script)
    if result.returncode != 0:
        log.error("shortcut %r failed: %s", name, result.stderr.strip()[:300])
        return False
    log.info("shortcut %r -> %s (%s)", name, hotkey, args)
    return True


def _existing_hotkeys() -> dict[str, str]:
    """Hotkeys already claimed by OTHER Start Menu shortcuts (conflict scan)."""
    script = r"""
$sh = New-Object -ComObject WScript.Shell
$roots = @([Environment]::GetFolderPath('StartMenu'),
           [Environment]::GetFolderPath('CommonStartMenu'))
foreach ($root in $roots) {
  Get-ChildItem -Path $root -Recurse -Filter *.lnk -ErrorAction SilentlyContinue |
    ForEach-Object {
      $k = $sh.CreateShortcut($_.FullName).Hotkey
      if ($k) { Write-Output "$k|$($_.FullName)" }
    }
}
"""
    found: dict[str, str] = {}
    try:
        result = _ps(script)
        for line in (result.stdout or "").splitlines():
            key, _, path = line.partition("|")
            if key.strip() and str(FOLDER).lower() not in path.lower():
                found[key.strip().upper()] = path.strip()
    except Exception as exc:
        log.warning("hotkey conflict scan failed: %s", exc)
    return found


def install() -> int:
    """Create all five shortcuts. Returns a process exit code."""
    FOLDER.mkdir(parents=True, exist_ok=True)
    keys = load_config()["shortcut_keys"]

    conflicts = _existing_hotkeys()
    failed = []
    for name, (args, description, key_name) in SHORTCUTS.items():
        hotkey = keys.get(key_name, "")
        other = conflicts.get(hotkey.upper())
        if other:
            log.warning("hotkey %s already used by %s — installing anyway; "
                        "Windows resolves to one of them", hotkey, other)
            print(f"CONFLICT: {hotkey} is also assigned to {other}")
        if not _create_one(name, args, description, hotkey):
            failed.append(name)

    if failed:
        print(f"FAILED: {', '.join(failed)} — see Logs/lifecycle.log")
        return 1
    print(f"Installed {len(SHORTCUTS)} shortcuts in {FOLDER}")
    for name, (_a, _d, key_name) in SHORTCUTS.items():
        print(f"  {keys.get(key_name, ''):22} {name}")
    print("Note: Windows activates .lnk hotkeys within a few seconds; "
          "if a key does nothing, open the Start Menu once to refresh.")
    return 0


def remove() -> int:
    """Delete only the shortcuts this project created (our folder only)."""
    if not FOLDER.exists():
        print("Nothing to remove.")
        return 0
    removed = 0
    for name in SHORTCUTS:
        lnk = FOLDER / f"{name}.lnk"
        try:
            lnk.unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.error("could not remove %s: %s", lnk, exc)
    try:
        FOLDER.rmdir()  # only succeeds when empty — never rm -rf
    except OSError:
        pass
    print(f"Removed {removed} shortcut(s).")
    log.info("removed %d shortcuts", removed)
    return 0


def repair() -> int:
    remove()
    return install()


def demo() -> None:
    """Self-check without touching the real Start Menu folder."""
    target, prefix = _launcher_target()
    assert Path(target).name in ("assistant_launcher.exe", "pythonw.exe", "python.exe")
    assert all(len(v) == 3 for v in SHORTCUTS.values())
    keys = load_config()["shortcut_keys"]
    assert keys["shutdown_all"] == "Ctrl+Alt+Shift+X"
    assert "JARVIS FRIDAY" in str(FOLDER)
    print("shortcut_installer demo OK:", target)


if __name__ == "__main__":
    demo()
