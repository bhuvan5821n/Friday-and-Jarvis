"""Deterministic tool registry.

The rule this module exists to enforce: **an unrecognised tool name produces an
error, never an execution.** Before this, `agent/executor.py` responded to an
unknown tool by asking a language model to write Python and running it in a
subprocess — so a hallucinated tool name was arbitrary code execution.

Resolution is exact-match on a normalized name, plus a closed alias table.
Fuzzy matching exists here but is strictly advisory: `suggest()` returns a name
to show the user, and no code path turns a suggestion into a call. "Did you
mean open_chrome?" is help; running open_chrome is a decision only the user
makes.
"""
from __future__ import annotations

import difflib
import re
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class RiskLevel(str, Enum):
    """How much damage a tool can do if it runs when it should not."""

    READ_ONLY = "read_only"      # observes; changes nothing
    LOW = "low"                  # reversible, local, no external effect
    MEDIUM = "medium"            # changes app or file state
    HIGH = "high"                # external effect, or hard to reverse
    CRITICAL = "critical"        # power, credentials, deletion, sending


class Permission(str, Enum):
    """What must be true before a tool may run."""

    ALLOWED = "allowed"                    # runs on request
    CONFIRM = "confirm"                    # needs explicit user confirmation
    CREATOR_ONLY = "creator_only"          # local/authenticated origin only
    FORBIDDEN = "forbidden"                # registered so it can be refused


class ToolNotRegistered(LookupError):
    """Raised instead of falling back to code generation.

    Carries a suggestion when one is close enough to be useful, so the caller
    can say "did you mean X" without ever being able to call X itself.
    """

    def __init__(self, name: str, suggestion: str | None = None) -> None:
        self.tool_name = name
        self.suggestion = suggestion
        self.error_code = "TOOL_NOT_REGISTERED"
        message = f"The tool '{name}' is not registered."
        if suggestion:
            message += f" Did you mean '{suggestion}'?"
        super().__init__(message)

    def as_result(self) -> dict[str, Any]:
        """Structured error for a model or a transport to receive."""
        return {
            "error_code": self.error_code,
            "tool": self.tool_name,
            "suggestion": self.suggestion,
            "message": str(self),
            "executed": False,
        }


# A tool name is an identifier, nothing else. Anything that is not [a-z0-9_]
# after normalization cannot address a tool, which keeps path fragments,
# shell metacharacters and unicode lookalikes out of resolution entirely.
_NAME_OK = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_NAME_CLEAN = re.compile(r"[\s\-.]+")


def normalize(raw: object) -> str:
    """Lowercase, collapse separators, reject anything else.

    Returns "" for a name that could never be valid — callers treat that the
    same as unknown, so a malformed name is refused rather than coerced into
    something that happens to match.
    """
    if not isinstance(raw, str):
        return ""
    name = _NAME_CLEAN.sub("_", raw.strip().lower())
    return name if _NAME_OK.match(name) else ""


@dataclass(frozen=True)
class ToolDefinition:
    canonical_name: str
    handler: Callable[..., Any]
    summary: str
    risk_level: RiskLevel = RiskLevel.MEDIUM
    permission: Permission = Permission.ALLOWED
    aliases: tuple[str, ...] = ()
    input_schema: dict[str, type] = field(default_factory=dict)
    required_params: tuple[str, ...] = ()
    timeout_seconds: float = 60.0
    supports_cancellation: bool = False
    supports_verification: bool = False
    verifier: str | None = None
    enabled: bool = True
    provenance: str = "builtin"

    #: Some action functions take a `speak` callback, some do not. Recorded as
    #: data rather than discovered by introspection, so a signature change is a
    #: visible edit here instead of a silent TypeError at runtime.
    accepts_speak: bool = False

    #: What to report when the handler returns nothing. Not a success claim —
    #: Phase 3 verification decides that; this only keeps the text honest.
    default_result: str = "Done."

    def validate(self, params: dict[str, Any]) -> None:
        """Shape check only. Semantic validation belongs to the handler."""
        missing = [p for p in self.required_params if not params.get(p)]
        if missing:
            raise ValueError(
                f"{self.canonical_name} requires: {', '.join(missing)}.")
        for key, expected in self.input_schema.items():
            if key in params and params[key] is not None:
                if not isinstance(params[key], expected):
                    raise ValueError(
                        f"{self.canonical_name}.{key} must be "
                        f"{expected.__name__}.")


class ToolRegistry:
    """Name → definition. Exact match or nothing."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._tools: dict[str, ToolDefinition] = {}
        self._aliases: dict[str, str] = {}

    def register(self, definition: ToolDefinition, *, replace: bool = False) -> None:
        name = normalize(definition.canonical_name)
        if not name:
            raise ValueError(
                f"invalid tool name: {definition.canonical_name!r}")
        with self._lock:
            if not replace and name in self._tools:
                raise ValueError(f"tool already registered: {name}")
            self._tools[name] = definition
            for alias in definition.aliases:
                key = normalize(alias)
                if key and key not in self._tools:
                    self._aliases[key] = name

    def resolve(self, tool_name: object) -> ToolDefinition | None:
        """Exact resolution. Never guesses."""
        key = normalize(tool_name)
        if not key:
            return None
        with self._lock:
            found = self._tools.get(key) or self._tools.get(
                self._aliases.get(key, ""))
        if found is None or not found.enabled:
            return None
        return found

    def contains(self, tool_name: object) -> bool:
        return self.resolve(tool_name) is not None

    def require(self, tool_name: object) -> ToolDefinition:
        """Resolve or raise. The only way execution paths should look tools up."""
        found = self.resolve(tool_name)
        if found is None:
            raw = tool_name if isinstance(tool_name, str) else repr(tool_name)
            raise ToolNotRegistered(raw, self.suggest(raw))
        return found

    def suggest(self, tool_name: object) -> str | None:
        """A close name to *show the user*.

        ponytail: advisory only, and deliberately never wired to execution —
        auto-running the nearest match is how a typo becomes an unintended
        action. difflib is stdlib and good enough; no fuzzy-match dependency.
        """
        key = normalize(tool_name)
        if not key:
            return None
        with self._lock:
            candidates = list(self._tools) + list(self._aliases)
        matches = difflib.get_close_matches(key, candidates, n=1, cutoff=0.75)
        if not matches:
            return None
        with self._lock:
            return self._aliases.get(matches[0], matches[0])

    def names(self) -> tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._tools))

    def describe(self) -> str:
        with self._lock:
            tools = sorted(self._tools.values(), key=lambda t: t.canonical_name)
        lines = ["Registered tools:"]
        for tool in tools:
            mark = "" if tool.permission is Permission.ALLOWED else \
                f" ({tool.permission.value})"
            lines.append(f"  {tool.canonical_name} — {tool.summary}{mark}")
        return "\n".join(lines)

    def clear(self) -> None:
        with self._lock:
            self._tools.clear()
            self._aliases.clear()


registry = ToolRegistry()


def _lazy(module: str, attr: str, accepts_speak: bool) -> Callable[..., Any]:
    """Import the action module on first call.

    The assistant's action modules pull in playwright, cv2 and pyautogui at
    import time. Registering them eagerly would move that cost to startup.
    """
    def handler(parameters: dict[str, Any], speak: Callable | None = None,
                player: Any = None) -> Any:
        import importlib
        func = getattr(importlib.import_module(module), attr)
        if accepts_speak:
            return func(parameters=parameters, player=player, speak=speak)
        return func(parameters=parameters, player=player)
    handler.__name__ = attr
    return handler


def _builtin(name: str, module: str, attr: str, summary: str, *,
             risk: RiskLevel = RiskLevel.MEDIUM,
             permission: Permission = Permission.ALLOWED,
             required: tuple[str, ...] = (),
             verifier: str | None = None,
             speak: bool = False,
             default_result: str = "Done.",
             aliases: tuple[str, ...] = ()) -> ToolDefinition:
    return ToolDefinition(
        canonical_name=name,
        handler=_lazy(module, attr, speak),
        summary=summary,
        risk_level=risk,
        permission=permission,
        required_params=required,
        verifier=verifier,
        supports_verification=verifier is not None,
        accepts_speak=speak,
        default_result=default_result,
        aliases=aliases,
    )


#: The complete agent-reachable vocabulary. Mirrors the tools that
#: `agent/executor.py` dispatched before this module existed — minus
#: `generated_code`, which was the vulnerability.
_BUILTINS: tuple[ToolDefinition, ...] = (
    _builtin("open_app", "actions.open_app", "open_app",
             "Open an application", risk=RiskLevel.LOW,
             required=("app_name",), verifier="application_open",
             aliases=("open_application", "launch_app")),
    _builtin("web_search", "actions.web_search", "web_search",
             "Search the web", risk=RiskLevel.READ_ONLY,
             verifier="web_search", aliases=("search",)),
    _builtin("game_updater", "actions.game_updater", "game_updater",
             "Check for game updates", risk=RiskLevel.READ_ONLY, speak=True),
    _builtin("browser_control", "actions.browser_control", "browser_control",
             "Drive the browser", risk=RiskLevel.HIGH,
             verifier="browser_navigation"),
    _builtin("file_controller", "actions.file_controller", "file_controller",
             "Read, write and organise files", risk=RiskLevel.HIGH,
             verifier="file_operation"),
    _builtin("code_helper", "actions.code_helper", "code_helper",
             "Explain or write code as text", risk=RiskLevel.READ_ONLY,
             speak=True),
    _builtin("dev_agent", "actions.dev_agent", "dev_agent",
             "Development assistance", risk=RiskLevel.HIGH, speak=True),
    _builtin("screen_process", "actions.screen_processor", "screen_process",
             "Capture and analyse the screen", risk=RiskLevel.MEDIUM,
             default_result="Screen captured and analyzed."),
    _builtin("send_message", "actions.send_message", "send_message",
             "Send a message", risk=RiskLevel.HIGH,
             permission=Permission.CONFIRM),
    _builtin("reminder", "actions.reminder", "reminder",
             "Set a reminder", risk=RiskLevel.LOW),
    _builtin("youtube_video", "actions.youtube_video", "youtube_video",
             "Play or summarise a video", risk=RiskLevel.LOW),
    _builtin("weather_report", "actions.weather_report", "weather_action",
             "Report the weather", risk=RiskLevel.READ_ONLY),
    _builtin("computer_settings", "actions.computer_settings",
             "computer_settings", "Change system settings",
             risk=RiskLevel.CRITICAL, permission=Permission.CONFIRM),
    _builtin("desktop_control", "actions.desktop", "desktop_control",
             "Desktop automation", risk=RiskLevel.HIGH),
    _builtin("computer_control", "actions.computer_control",
             "computer_control", "Mouse and keyboard control",
             risk=RiskLevel.HIGH),
    _builtin("flight_finder", "actions.flight_finder", "flight_finder",
             "Search for flights", risk=RiskLevel.READ_ONLY, speak=True),
)


def install_builtins(target: ToolRegistry | None = None) -> ToolRegistry:
    target = target or registry
    for definition in _BUILTINS:
        target.register(definition, replace=True)
    return target


install_builtins()
