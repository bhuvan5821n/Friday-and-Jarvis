"""Phase 10: the safe command registry.

WhatsApp must never be able to run arbitrary shell text on this laptop. The
only commands that exist are the ones written down here, by hand, in this file:

  * each entry is a fixed argv list — never a shell string, never `shell=True`
  * user text is never interpolated into a command; a command either takes no
    argument or takes one from a closed set defined alongside it
  * dangerous entries are flagged and must go through the confirmation state
    machine before they run — this module refuses to run them directly
  * WhatsApp cannot add, edit, or enable entries. Adding a command means
    editing this file, on the laptop, and re-running the tests.

Free-form shell is refused with a fixed sentence rather than silently ignored,
so Bhuvan knows the boundary exists rather than wondering why nothing happened.
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

log = logging.getLogger("nexus.commands")

#: The exact refusal for any attempt to run shell text from WhatsApp.
SHELL_REFUSAL = ("I cannot execute arbitrary remote shell text. I can run "
                 "approved commands or a locally reviewed command macro.")

#: No approved command should ever take this long. Beyond it the child is
#: killed rather than left holding the bridge thread.
DEFAULT_TIMEOUT = 20.0


class CommandError(RuntimeError):
    """The command could not run. The message is safe to send back."""


@dataclass(frozen=True)
class SafeCommand:
    """One approved command.

    `argv` is the complete command line. `choices`, when set, is the closed set
    of values allowed for the single `{arg}` placeholder — the only way user
    input ever reaches a command line, and it must match a listed value exactly.
    """

    name: str
    argv: tuple[str, ...]
    summary: str
    dangerous: bool = False
    choices: tuple[str, ...] = ()
    timeout: float = DEFAULT_TIMEOUT

    def build(self, arg: str | None = None) -> list[str]:
        """The concrete argv, with the argument validated against `choices`."""
        if not self.choices:
            if arg:
                raise CommandError(f"{self.name} does not take an argument.")
            return list(self.argv)

        if arg is None:
            raise CommandError(f"{self.name} needs one of: "
                               f"{', '.join(self.choices)}.")
        # Exact membership, not pattern matching: this is the one place user
        # text becomes part of a command line.
        if arg not in self.choices:
            raise CommandError(f"'{arg}' is not an allowed value for "
                               f"{self.name}. Allowed: {', '.join(self.choices)}.")
        return [part.replace("{arg}", arg) for part in self.argv]


#: The complete vocabulary. Everything reachable from WhatsApp is on this list.
#: Read-only entries run immediately; dangerous ones require confirmation.
REGISTRY: dict[str, SafeCommand] = {
    cmd.name: cmd for cmd in (
        SafeCommand(
            name="lock",
            argv=("rundll32.exe", "user32.dll,LockWorkStation"),
            summary="Lock the screen"),
        SafeCommand(
            name="sleep",
            argv=("rundll32.exe", "powrprof.dll,SetSuspendState", "0,1,0"),
            summary="Put the laptop to sleep", dangerous=True),
        SafeCommand(
            name="shutdown",
            argv=("shutdown.exe", "/s", "/t", "60"),
            summary="Shut down the laptop in 60 seconds", dangerous=True),
        SafeCommand(
            name="restart",
            argv=("shutdown.exe", "/r", "/t", "60"),
            summary="Restart the laptop in 60 seconds", dangerous=True),
        SafeCommand(
            name="cancel_shutdown",
            argv=("shutdown.exe", "/a"),
            summary="Cancel a pending shutdown or restart"),
        SafeCommand(
            name="uptime",
            argv=("net.exe", "statistics", "workstation"),
            summary="How long the laptop has been up"),
        SafeCommand(
            name="wifi_status",
            argv=("netsh.exe", "wlan", "show", "interfaces"),
            summary="Wi-Fi connection details"),
        SafeCommand(
            name="battery_report",
            argv=("wmic.exe", "path", "Win32_Battery", "get",
                  "EstimatedChargeRemaining,BatteryStatus", "/format:list"),
            summary="Battery detail from the hardware"),
        SafeCommand(
            name="top_processes",
            argv=("tasklist.exe", "/fo", "csv", "/nh"),
            summary="What is running"),
    )
}


def describe() -> str:
    """The list Bhuvan can ask for. Nothing outside it can be run."""
    lines = ["Approved remote commands:"]
    for cmd in REGISTRY.values():
        mark = " (needs confirmation)" if cmd.dangerous else ""
        lines.append(f"  {cmd.name} — {cmd.summary}{mark}")
    return "\n".join(lines)


def lookup(name: str) -> SafeCommand | None:
    return REGISTRY.get(str(name or "").strip().lower())


def run(name: str, arg: str | None = None, *, allow_dangerous: bool = False
        ) -> str:
    """Run an approved command and return its output.

    `allow_dangerous` is passed only by the confirmation path. A dangerous
    command reached any other way is refused here, so a missing confirmation
    check upstream fails closed rather than executing.
    """
    command = lookup(name)
    if command is None:
        raise CommandError(SHELL_REFUSAL)
    if command.dangerous and not allow_dangerous:
        raise CommandError(f"{command.summary} needs confirmation first.")

    argv = command.build(arg)
    started = time.monotonic()
    try:
        # shell=False is the default and must stay that way: argv is a list, so
        # no part of it is ever parsed by a shell.
        completed = subprocess.run(argv, capture_output=True, text=True,
                                   timeout=command.timeout,
                                   creationflags=getattr(subprocess,
                                                         "CREATE_NO_WINDOW", 0))
    except FileNotFoundError:
        raise CommandError(f"{command.name} is not available on this system.")
    except subprocess.TimeoutExpired:
        raise CommandError(f"{command.name} took too long and was stopped.")
    except OSError as exc:
        log.warning("%s failed to start: %s", command.name, exc)
        raise CommandError(f"{command.name} could not be started.")

    elapsed = int((time.monotonic() - started) * 1000)
    log.info("ran %s in %dms (rc=%d)", command.name, elapsed,
             completed.returncode)

    output = (completed.stdout or "").strip()
    if completed.returncode != 0 and not output:
        detail = (completed.stderr or "").strip()
        raise CommandError(f"{command.name} failed"
                           + (f": {detail[:120]}" if detail else "."))
    return output or f"{command.summary} — done."
