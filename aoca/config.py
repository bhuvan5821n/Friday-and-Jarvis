"""Feature flags and tunable limits.

Flags resolve environment first, then `config/api_keys.json` — the same
precedence the assistant already uses for its own settings (`main.py:105`), so
there is one mental model rather than two.

Everything adaptive is off. The learning flags are not merely defaulted false:
`_LOCKED_OFF` cannot be turned on by config at all, because a half-built
subsystem that can be switched on from a JSON file is a subsystem that will be
switched on by accident.
"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = _base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"

#: Phases 1-3. Default on — they only make the system more honest.
_DEFAULTS = {
    "AOCA_ENABLED": True,
    "AOCA_EVENTS_ENABLED": True,
    "AOCA_VERIFICATION_ENABLED": True,
    "AOCA_OUTCOME_STORAGE_ENABLED": True,
    # Phase 4-6: on by default when master is on; cognitive graph is the point.
    "AOCA_GRAPH_ENABLED": True,
    "AOCA_MEMORY_ENABLED": True,
    "AOCA_RETRIEVAL_ENABLED": True,
    "AOCA_ACTIVATION_ENABLED": True,
    "AOCA_MEMORY_ADMISSION_ENABLED": True,
    # Off until tested: consolidation runs in the background.
    "AOCA_CONSOLIDATION_ENABLED": False,
}

#: Not implemented. Reading any of these returns False no matter what the
#: environment or config says. Remove a name from this set only in the phase
#: that actually builds the subsystem behind it.
_LOCKED_OFF = frozenset({
    "AOCA_LEARNING_ENABLED",
    "AOCA_WORLD_MODEL_ENABLED",
    "AOCA_PLANNER_ENABLED",
    "AOCA_NEURAL_CORE_ENABLED",
    "AOCA_BANDIT_ENABLED",
})

_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def _coerce(value: object) -> bool | None:
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in _TRUE:
        return True
    if text in _FALSE:
        return False
    return None


class Flags:
    """Flag reader with a short cache.

    Config is re-read at most every `_TTL` seconds. Without the cache every
    policy decision would hit the disk; without the TTL a flag change would
    need a restart. Both matter — `AOCA_ENABLED=false` is the operator's brake
    and must take effect while the assistant is running.
    """

    _TTL = 5.0

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._cache: dict[str, object] = {}
        self._loaded_at = 0.0
        self._overrides: dict[str, bool] = {}

    def _config(self) -> dict[str, object]:
        import time
        now = time.monotonic()
        with self._lock:
            if self._cache and (now - self._loaded_at) < self._TTL:
                return self._cache
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                self._cache = data if isinstance(data, dict) else {}
            except (OSError, ValueError):
                # A missing or malformed config is not an error here: every
                # flag has a default, and failing to read it must not stop the
                # assistant from starting.
                self._cache = {}
            self._loaded_at = now
            return self._cache

    def get(self, name: str) -> bool:
        if name in _LOCKED_OFF:
            return False

        with self._lock:
            if name in self._overrides:
                return self._overrides[name]

        env = _coerce(os.environ.get(name, ""))
        if env is not None:
            return env

        cfg = _coerce(self._config().get(name, ""))
        if cfg is not None:
            return cfg

        return _DEFAULTS.get(name, False)

    def __getattr__(self, name: str) -> bool:
        if name.startswith("_"):
            raise AttributeError(name)
        return self.get(name if name.startswith("AOCA_") else f"AOCA_{name.upper()}")

    def enabled(self, name: str) -> bool:
        """A sub-flag is only live when the master flag is."""
        return self.get("AOCA_ENABLED") and self.get(name)

    def set_override(self, name: str, value: bool | None) -> None:
        """In-process override. For tests and for a runtime kill switch."""
        with self._lock:
            if value is None:
                self._overrides.pop(name, None)
            else:
                self._overrides[name] = bool(value)

    def clear_overrides(self) -> None:
        with self._lock:
            self._overrides.clear()

    def snapshot(self) -> dict[str, bool]:
        names = sorted(set(_DEFAULTS) | set(_LOCKED_OFF))
        return {name: self.get(name) for name in names}


flags = Flags()


class Limits:
    """Bounded resources. Data, not literals scattered through the code."""

    #: Pending telemetry events held before low-value ones are dropped.
    EVENT_QUEUE_MAX = 512

    #: Longest a single verification may take before reporting UNVERIFIED.
    VERIFY_TIMEOUT_SECONDS = 12.0

    #: Poll interval while waiting for evidence. Not a busy loop.
    VERIFY_POLL_SECONDS = 0.25

    #: A process that dies within this window after launch is treated as
    #: STARTED_THEN_EXITED rather than a success.
    PROCESS_EARLY_EXIT_SECONDS = 3.0

    #: Longest a launched application may take to show evidence.
    APP_LAUNCH_TIMEOUT_SECONDS = 15.0

    #: Truncation for any free text that reaches an event or the database.
    SAFE_TEXT_MAX = 400

    #: Rows kept in action_outcomes before the oldest are pruned.
    OUTCOME_ROWS_MAX = 50_000


limits = Limits()
