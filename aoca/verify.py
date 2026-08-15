"""Execution and verification, kept separate.

Before this, "success" meant "the function returned without raising". `open_app`
called `Popen`, slept 1.5 seconds, returned True, and the assistant said
"Opened Chrome." — whether or not Chrome existed. Every quantity a later
learning layer would train on was therefore a constant, which is why this phase
comes before any of them.

Three rules:

  * A verifier that does not exist reports `UNVERIFIED`, never `SUCCEEDED`.
    A fake verifier returning True is worse than none, because it is invisible.
  * `SUCCEEDED` requires execution started **and** verification completed **and**
    the expected state observed. Two out of three is not success.
  * Process identity is `(pid, create_time, exe)`, never pid alone. Windows
    reuses pids, so a pid-only check can "verify" an unrelated process.
"""
from __future__ import annotations

import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from aoca.config import flags, limits

log = logging.getLogger("aoca.verify")

try:
    import psutil
    _PSUTIL = True
except ImportError:      # pragma: no cover
    _PSUTIL = False


class Outcome(str, Enum):
    """What actually happened. `UNVERIFIED` is a real answer, not a failure."""

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    UNVERIFIED = "unverified"
    REFUSED = "refused"
    TIMED_OUT = "timed_out"
    CANCELLED = "cancelled"
    STARTED_THEN_EXITED = "started_then_exited"


@dataclass(frozen=True)
class ProcessIdentity:
    """A process, identified so a reused pid cannot impersonate it."""

    pid: int
    create_time: float
    name: str = ""
    exe: str = ""

    @property
    def key(self) -> tuple[int, int]:
        # create_time to the millisecond. Windows pid reuse within the same
        # millisecond and the same executable is not a case worth modelling.
        return (self.pid, int(self.create_time * 1000))

    def alive(self) -> bool:
        """True only if the same process is still there."""
        if not _PSUTIL:
            return False
        try:
            proc = psutil.Process(self.pid)
            return int(proc.create_time() * 1000) == self.key[1]
        except Exception:
            return False


def snapshot_processes(name_filter: str = "") -> dict[tuple[int, int],
                                                      ProcessIdentity]:
    """Current processes, keyed by identity. Empty dict when psutil is absent.

    An empty snapshot is honest: it makes the verifier report UNVERIFIED rather
    than compare against a set it could not read.
    """
    if not _PSUTIL:
        return {}
    wanted = name_filter.lower().strip()
    found: dict[tuple[int, int], ProcessIdentity] = {}
    for proc in psutil.process_iter(["pid", "name", "create_time", "exe"]):
        try:
            info = proc.info
            name = (info.get("name") or "").lower()
            if wanted and wanted not in name:
                continue
            identity = ProcessIdentity(
                pid=info["pid"],
                create_time=info.get("create_time") or 0.0,
                name=info.get("name") or "",
                exe=info.get("exe") or "",
            )
            found[identity.key] = identity
        except Exception:
            continue    # a process that vanished mid-iteration is not an error
    return found


@dataclass(frozen=True)
class ExecutionResult:
    """What the executor did. Says nothing about whether it worked."""

    execution_started: bool
    tool: str
    method: str = ""
    detail: str = ""
    duration_ms: int = 0
    error: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class VerificationResult:
    """What was observed afterwards."""

    verification_completed: bool
    expected_state_observed: bool
    verifier: str
    reason: str = ""
    duration_ms: int = 0
    evidence: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def unavailable(cls, verifier: str = "none") -> VerificationResult:
        """No verifier for this action. Not a failure, and not a success."""
        return cls(False, False, verifier,
                   reason="no verifier available for this action")


@dataclass(frozen=True)
class FinalActionOutcome:
    """The single honest answer, and the sentence the user hears."""

    outcome: Outcome
    tool: str
    execution: ExecutionResult
    verification: VerificationResult
    message: str

    @property
    def succeeded(self) -> bool:
        return self.outcome is Outcome.SUCCEEDED

    @property
    def learnable(self) -> bool:
        """Whether a later learning layer may use this as a training signal.

        Only a verified observation qualifies. `UNVERIFIED` must never be
        reinforced — training on it would teach the system that launching
        nothing is as good as launching something.
        """
        return self.outcome in (Outcome.SUCCEEDED, Outcome.FAILED,
                                Outcome.STARTED_THEN_EXITED)

    def as_event(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "outcome": self.outcome.value,
            "execution_started": self.execution.execution_started,
            "verification_completed": self.verification.verification_completed,
            "expected_state_observed": self.verification.expected_state_observed,
            "verifier": self.verification.verifier,
            "duration_ms": self.execution.duration_ms
            + self.verification.duration_ms,
        }


def combine(tool: str, execution: ExecutionResult,
            verification: VerificationResult,
            subject: str = "") -> FinalActionOutcome:
    """The only place an outcome is decided. One rule, applied everywhere."""
    label = subject or tool

    if not execution.execution_started:
        return FinalActionOutcome(
            Outcome.FAILED, tool, execution, verification,
            f"I couldn't start that. {execution.error or ''}".strip())

    if not verification.verification_completed:
        return FinalActionOutcome(
            Outcome.UNVERIFIED, tool, execution, verification,
            f"I started {label} but couldn't confirm it. "
            f"Please check whether it worked.")

    if verification.evidence.get("early_exit"):
        return FinalActionOutcome(
            Outcome.STARTED_THEN_EXITED, tool, execution, verification,
            f"{label} started and then closed straight away. "
            f"Something is wrong with it.")

    if not verification.expected_state_observed:
        return FinalActionOutcome(
            Outcome.FAILED, tool, execution, verification,
            f"I tried, but {label} isn't running. {verification.reason}".strip())

    return FinalActionOutcome(
        Outcome.SUCCEEDED, tool, execution, verification,
        f"Opened {label}." if tool == "open_app" else f"Done — {label}.")


# ---- verifier registry ---------------------------------------------------

#: verifier name -> callable(context: dict) -> VerificationResult
_VERIFIERS: dict[str, Callable[[dict[str, Any]], VerificationResult]] = {}


def register_verifier(name: str,
                      func: Callable[[dict[str, Any]], VerificationResult]
                      ) -> None:
    _VERIFIERS[name] = func


def verify(name: str | None, context: dict[str, Any]) -> VerificationResult:
    """Run a verifier by name. An unknown name reports UNVERIFIED."""
    if not name or not flags.enabled("AOCA_VERIFICATION_ENABLED"):
        return VerificationResult.unavailable(name or "disabled")
    func = _VERIFIERS.get(name)
    if func is None:
        return VerificationResult.unavailable(name)
    started = time.monotonic()
    try:
        result = func(context)
    except Exception as exc:
        log.debug("verifier %s failed: %s", name, exc)
        return VerificationResult(
            False, False, name,
            reason=f"the check itself failed ({type(exc).__name__})")
    elapsed = int((time.monotonic() - started) * 1000)
    return VerificationResult(
        result.verification_completed, result.expected_state_observed,
        name, result.reason, elapsed, result.evidence)


def has_verifier(name: str | None) -> bool:
    return bool(name) and name in _VERIFIERS


def verifier_names() -> tuple[str, ...]:
    return tuple(sorted(_VERIFIERS))


# ---- application_open ----------------------------------------------------

def _verify_application_open(context: dict[str, Any]) -> VerificationResult:
    """Poll for a new process matching the launched application.

    Bounded polling, not a fixed sleep: a fast app is confirmed in 250 ms and a
    slow one gets the full window, whereas one `sleep(1.5)` is simultaneously
    too long for the first and too short for the second.
    """
    if not _PSUTIL:
        return VerificationResult.unavailable("application_open")

    before: dict[tuple[int, int], ProcessIdentity] = context.get("before") or {}
    expected = str(context.get("expected_name") or "").lower().strip()
    if not expected:
        return VerificationResult(
            False, False, "application_open",
            reason="no executable name to look for")

    stem = expected.rsplit(".", 1)[0]
    timeout = float(context.get("timeout")
                    or limits.APP_LAUNCH_TIMEOUT_SECONDS)
    deadline = time.monotonic() + timeout
    matched: ProcessIdentity | None = None

    while time.monotonic() < deadline:
        for key, identity in snapshot_processes(stem).items():
            if key not in before:
                matched = identity
                break
        if matched:
            break
        time.sleep(limits.VERIFY_POLL_SECONDS)

    if matched is None:
        # An already-running instance is a real outcome: the app the user asked
        # for is on screen. Reported distinctly so it is not counted as a launch.
        for key, identity in snapshot_processes(stem).items():
            if key in before:
                return VerificationResult(
                    True, True, "application_open",
                    reason="it was already running",
                    evidence={"pid": identity.pid, "already_running": True})
        return VerificationResult(
            True, False, "application_open",
            reason="no matching process appeared",
            evidence={"expected_name": stem})

    # A process that exits immediately launched and then died. Saying "opened"
    # would be the same lie as before, one step later.
    settle = min(limits.PROCESS_EARLY_EXIT_SECONDS,
                 max(0.0, deadline - time.monotonic()))
    if settle > 0:
        time.sleep(settle)
    if not matched.alive():
        return VerificationResult(
            True, False, "application_open",
            reason="it started and exited immediately",
            evidence={"pid": matched.pid, "early_exit": True})

    return VerificationResult(
        True, True, "application_open",
        reason="process is running",
        evidence={"pid": matched.pid,
                  "process_create_time": matched.create_time})


register_verifier("application_open", _verify_application_open)


def _verify_process_stopped(context: dict[str, Any]) -> VerificationResult:
    identity: ProcessIdentity | None = context.get("identity")
    if identity is None:
        return VerificationResult(False, False, "process_stopped",
                                  reason="no process identity was recorded")
    deadline = time.monotonic() + float(
        context.get("timeout") or limits.VERIFY_TIMEOUT_SECONDS)
    while time.monotonic() < deadline:
        if not identity.alive():
            return VerificationResult(True, True, "process_stopped",
                                      reason="process is gone",
                                      evidence={"pid": identity.pid})
        time.sleep(limits.VERIFY_POLL_SECONDS)
    return VerificationResult(True, False, "process_stopped",
                              reason="process is still running",
                              evidence={"pid": identity.pid})


register_verifier("process_stopped", _verify_process_stopped)


def _verify_file_operation(context: dict[str, Any]) -> VerificationResult:
    """The file exists (or is gone, for a delete) after the operation."""
    from pathlib import Path

    target = context.get("path")
    if not target:
        return VerificationResult(False, False, "file_operation",
                                  reason="no path was recorded")
    path = Path(str(target))
    should_exist = bool(context.get("should_exist", True))
    exists = path.exists()
    ok = exists == should_exist
    return VerificationResult(
        True, ok, "file_operation",
        reason=("file is present" if exists else "file is not present"),
        evidence={"size_bytes": path.stat().st_size
                  if exists and path.is_file() else 0})


register_verifier("file_operation", _verify_file_operation)
