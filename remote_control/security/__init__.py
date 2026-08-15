"""Admission control. Pure stdlib — loaded from a foreign Python environment.

Two independent gates, both of which must pass before a WhatsApp message is
allowed to become an AI request:

  sender_gate  — is this message from the one authorized number?
  prefix_gate  — was this message actually addressed to an assistant?

Both fail closed. Neither ever returns message content in a rejection.
"""

from .admission import Admission, AdmissionController
from .prefix_gate import (ACCEPTED_PREFIXES, GateDecision, parse_prefix,
                          strip_prefix)
from .sender_gate import (AuthorizedSender, is_authorized, load_authorized,
                          normalize_number)

__all__ = ["Admission", "AdmissionController",
           "ACCEPTED_PREFIXES", "GateDecision", "parse_prefix", "strip_prefix",
           "AuthorizedSender", "is_authorized", "load_authorized",
           "normalize_number"]
