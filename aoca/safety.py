"""The safety kernel: one decision function, deny by default.

Generalized from `remote_control/security/` — that module already gets this
right for WhatsApp (exact sender match, exact prefix, fixed argv, dangerous
actions parked for confirmation). This is the same shape applied to every
origin, so the local voice path is not more trusting than the remote one.

Three invariants, each with a test:

  * An unexpected error while deciding is a **denial**, never a pass. A safety
    service that throws must not become a safety service that waves things
    through.
  * `Origin.UNTRUSTED_CONTENT` — webpage text, email bodies, model output — can
    never reach an action. Content is data. Only a person issues instructions.
  * No score, weight, or learned quantity appears anywhere in this file. There
    is nothing here for a later learning layer to move. A mask of zero cannot
    be argued up.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum

from aoca.tools import Permission, RiskLevel, ToolDefinition, ToolNotRegistered
from aoca.tools import registry as _default_registry


class Origin(str, Enum):
    """Where the instruction came from. Trust is a property of origin only."""

    LOCAL_UI = "local_ui"                    # typed into the app window
    LOCAL_VOICE = "local_voice"              # spoken at this machine
    REMOTE_AUTHORIZED = "remote_authorized"  # passed remote_control's gates
    SCHEDULED = "scheduled"                  # a reminder or timer firing
    UNTRUSTED_CONTENT = "untrusted_content"  # webpage, email, model output
    UNKNOWN = "unknown"                      # unattributed — treated as hostile


#: Origins that may cause an action at all. Everything else observes.
_ACTOR_ORIGINS = frozenset({
    Origin.LOCAL_UI, Origin.LOCAL_VOICE, Origin.REMOTE_AUTHORIZED,
    Origin.SCHEDULED,
})

#: Remote and scheduled origins may not reach these risk levels without a
#: person present, whatever the tool's own permission says.
_REMOTE_MAX_RISK = {
    Origin.REMOTE_AUTHORIZED: RiskLevel.HIGH,
    Origin.SCHEDULED: RiskLevel.MEDIUM,
}

_RISK_ORDER = {
    RiskLevel.READ_ONLY: 0, RiskLevel.LOW: 1, RiskLevel.MEDIUM: 2,
    RiskLevel.HIGH: 3, RiskLevel.CRITICAL: 4,
}


@dataclass(frozen=True)
class PolicyDecision:
    """The answer, and the reason in words safe to show or log.

    `safe_reason` never contains the request text — it names the rule that
    fired, so the decision can be logged and spoken without leaking content.
    """

    permitted: bool
    tool_name: str
    origin: Origin
    risk_level: RiskLevel
    confirmation_required: bool
    policy_rule: str
    safe_reason: str
    constraints: dict[str, object] = field(default_factory=dict)

    @property
    def denied(self) -> bool:
        return not self.permitted

    def as_event(self) -> dict[str, object]:
        """Flat, redaction-safe payload for the event bus."""
        return {
            "permitted": self.permitted,
            "tool": self.tool_name,
            "origin": self.origin.value,
            "risk": self.risk_level.value,
            "confirmation_required": self.confirmation_required,
            "policy_rule": self.policy_rule,
        }


def _deny(tool: str, origin: Origin, rule: str, reason: str,
          risk: RiskLevel = RiskLevel.CRITICAL) -> PolicyDecision:
    return PolicyDecision(
        permitted=False, tool_name=tool, origin=origin, risk_level=risk,
        confirmation_required=False, policy_rule=rule, safe_reason=reason)


class SafetyKernel:
    """Deterministic. Same inputs, same answer, every time."""

    def __init__(self, tool_registry=None) -> None:
        self._registry = tool_registry or _default_registry
        self._lock = threading.RLock()
        self._denials = 0

    @property
    def denial_count(self) -> int:
        with self._lock:
            return self._denials

    def decide(self, tool_name: object, origin: Origin | str = Origin.UNKNOWN,
               params: dict | None = None) -> PolicyDecision:
        try:
            return self._decide(tool_name, origin, params or {})
        except ToolNotRegistered as exc:
            return self._record(_deny(
                exc.tool_name, self._origin(origin), "tool_not_registered",
                "That is not something I can do. Nothing was run."))
        except Exception as exc:
            # Fail closed. An unexpected fault in the decision path is the
            # exact moment optimism is most expensive.
            return self._record(_deny(
                str(tool_name)[:64], Origin.UNKNOWN, "policy_error",
                f"Safety check failed ({type(exc).__name__}), so I did not "
                f"run it."))

    def _decide(self, tool_name: object, origin: Origin | str,
                params: dict) -> PolicyDecision:
        source = self._origin(origin)

        if source is Origin.UNTRUSTED_CONTENT:
            return self._record(_deny(
                str(tool_name)[:64], source, "untrusted_content_cannot_act",
                "That instruction came from page or message content, not from "
                "you, so I ignored it."))

        if source not in _ACTOR_ORIGINS:
            return self._record(_deny(
                str(tool_name)[:64], source, "unattributed_origin",
                "I could not tell where that request came from, so I did not "
                "act on it."))

        definition: ToolDefinition = self._registry.require(tool_name)

        if definition.permission is Permission.FORBIDDEN:
            return self._record(_deny(
                definition.canonical_name, source, "tool_forbidden",
                "That action is disabled.", definition.risk_level))

        if definition.permission is Permission.CREATOR_ONLY and source not in (
                Origin.LOCAL_UI, Origin.LOCAL_VOICE):
            return self._record(_deny(
                definition.canonical_name, source, "creator_only",
                "That one I only do from this machine.",
                definition.risk_level))

        ceiling = _REMOTE_MAX_RISK.get(source)
        if ceiling is not None and (
                _RISK_ORDER[definition.risk_level] > _RISK_ORDER[ceiling]):
            where = ("a remote message" if source is Origin.REMOTE_AUTHORIZED
                     else "a scheduled task")
            return self._record(_deny(
                definition.canonical_name, source, "risk_above_origin_ceiling",
                f"That is too risky to do from {where}.",
                definition.risk_level))

        try:
            definition.validate(params)
        except ValueError as exc:
            return self._record(_deny(
                definition.canonical_name, source, "invalid_parameters",
                str(exc), definition.risk_level))

        confirm = definition.permission is Permission.CONFIRM or (
            definition.risk_level is RiskLevel.CRITICAL)

        return PolicyDecision(
            permitted=True,
            tool_name=definition.canonical_name,
            origin=source,
            risk_level=definition.risk_level,
            confirmation_required=confirm,
            policy_rule="allowed" if not confirm else "allowed_with_confirmation",
            safe_reason="Permitted.",
            constraints={"timeout_seconds": definition.timeout_seconds},
        )

    @staticmethod
    def _origin(origin: Origin | str) -> Origin:
        if isinstance(origin, Origin):
            return origin
        try:
            return Origin(str(origin).strip().lower())
        except ValueError:
            return Origin.UNKNOWN

    def _record(self, decision: PolicyDecision) -> PolicyDecision:
        with self._lock:
            self._denials += 1
        return decision


kernel = SafetyKernel()
