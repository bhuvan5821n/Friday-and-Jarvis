"""Sender gate — exactly one number may speak to this laptop.

Independent of Hermes' own allowlist on purpose. Hermes already filters in the
Node bridge and again in `_is_dm_intake_allowed`, but a control path this
dangerous should not depend on a single config value in someone else's project
being correct. This gate re-derives the answer from our own config.

Unauthorized senders get *nothing* — no reply, no error, no acknowledgement,
no hint that the laptop exists or is online. The only trace is a counter.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

#: Our own config, inside this project (never the Hermes install).
DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "remote_control.json"

_DIGITS_RE = re.compile(r"\D+")

#: WhatsApp JIDs look like "1234567890@s.whatsapp.net" or
#: "1234567890:12@s.whatsapp.net" (device-suffixed). Everything after the
#: first '@' or ':' is transport detail, not identity.
_JID_TAIL_RE = re.compile(r"[@:].*$", re.DOTALL)

#: Plausible E.164 range. Anything outside it cannot be a real phone number,
#: so treating it as unauthorized costs nothing.
_MIN_DIGITS, _MAX_DIGITS = 8, 15

#: WhatsApp's newer Linked Identity Device format ("67427329167522@lid").
#: A LID is *not* a phone number and must never be compared against one — the
#: digit strings are unrelated namespaces. It is matched only against LIDs
#: explicitly authorized in config.
_LID_RE = re.compile(r"^(\d{5,25})(?::\d+)?@lid$", re.IGNORECASE)


def normalize_lid(raw: object) -> str:
    """Bare LID digits, or "" when the value is not a LID JID."""
    if not isinstance(raw, str):
        return ""
    match = _LID_RE.match(raw.strip())
    return match.group(1) if match else ""


def normalize_number(raw: object) -> str:
    """Reduce a sender identifier to bare country-code digits.

    Accepts JIDs, spacing, dashes, brackets, and a leading '+'. Returns "" for
    anything that is not a plausible phone number — callers treat "" as
    unauthorized, so a parse failure can never widen access.

    A LID JID returns "" here: LID digits live in a different namespace and
    must never be compared against a phone number.
    """
    if not isinstance(raw, str):
        return ""
    if normalize_lid(raw):
        return ""
    text = _JID_TAIL_RE.sub("", raw.strip())
    digits = _DIGITS_RE.sub("", text)
    if not (_MIN_DIGITS <= len(digits) <= _MAX_DIGITS):
        return ""
    return digits


@dataclass(frozen=True)
class AuthorizedSender:
    """The allowlist. Empty means nobody — never everybody.

    Phone numbers and LIDs are separate sets because they are separate
    identity namespaces; a LID is only accepted if it was explicitly
    authorized, never because its digits resemble a phone number.
    """

    numbers: frozenset[str]
    lids: frozenset[str] = frozenset()

    @property
    def configured(self) -> bool:
        return bool(self.numbers or self.lids)

    def allows(self, raw_sender: object) -> bool:
        lid = normalize_lid(raw_sender)
        if lid:
            return lid in self.lids
        number = normalize_number(raw_sender)
        return bool(number) and number in self.numbers


def load_authorized(config_path: Path | str | None = None) -> AuthorizedSender:
    """Read the allowlist from our config. Returns an empty (deny-all) set on
    any problem — a missing or corrupt config must not open the gate.

    A wildcard entry is rejected outright rather than honoured: there is no
    legitimate reason for this laptop to accept commands from anyone.
    """
    path = Path(config_path) if config_path else DEFAULT_CONFIG
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return AuthorizedSender(frozenset())

    numbers = _clean(data.get("authorized_whatsapp_numbers"), normalize_number)
    lids = _clean(data.get("authorized_whatsapp_lids"),
                  lambda v: normalize_lid(v if "@" in v else f"{v}@lid"))
    return AuthorizedSender(numbers, lids)


def _clean(raw: object, normalize) -> frozenset[str]:
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return frozenset()
    out = set()
    for entry in raw:
        if not isinstance(entry, str) or "*" in entry:
            continue          # wildcards are never expanded
        value = normalize(entry)
        if value:
            out.add(value)
    return frozenset(out)


def is_authorized(raw_sender: object,
                  config_path: Path | str | None = None) -> bool:
    """One-shot check. Prefer holding an AuthorizedSender for hot paths."""
    return load_authorized(config_path).allows(raw_sender)
