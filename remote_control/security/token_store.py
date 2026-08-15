"""Bridge token storage — Windows Credential Manager, never a file, never Git.

The token authenticates the Hermes gateway to the assistant's remote-control
endpoint. Anyone holding it can drive this laptop, so it is treated as a
password: stored in the OS credential vault under the current user, generated
on first use, and never written to the repo, a config file, or a log.

`win32cred` ships with pywin32, which both this project and the Hermes
virtualenv already have — so the same store is reachable from both sides of the
IPC boundary without adding a dependency to either.
"""
from __future__ import annotations

import secrets

#: Credential Manager entry name. Generic credentials are scoped to the
#: Windows user, so another account on this machine cannot read it.
TARGET = "NEXUS/RemoteControl/BridgeToken"

_USERNAME = "nexus-bridge"

#: 32 bytes of urandom, hex-encoded. Long enough that guessing is irrelevant
#: and the comparison cost stays constant.
_TOKEN_BYTES = 32


class TokenUnavailable(RuntimeError):
    """The token could not be read or created. Callers must fail closed."""


def _win32cred():
    try:
        import win32cred
    except ImportError as exc:      # pragma: no cover - platform guard
        raise TokenUnavailable(
            "pywin32 is required to reach Windows Credential Manager") from exc
    return win32cred


def read_token() -> str | None:
    """The stored token, or None when no credential exists yet."""
    win32cred = _win32cred()
    try:
        cred = win32cred.CredRead(TARGET, win32cred.CRED_TYPE_GENERIC, 0)
    except Exception:
        return None                 # not found, or not readable by this user
    blob = cred.get("CredentialBlob")
    if not blob:
        return None
    # CredWrite stores UTF-16-LE; decode symmetrically with how we wrote it.
    token = blob.decode("utf-16-le") if isinstance(blob, bytes) else str(blob)
    return token.strip() or None


def write_token(token: str) -> None:
    """Store (or replace) the token in Credential Manager."""
    win32cred = _win32cred()
    try:
        win32cred.CredWrite({
            "Type": win32cred.CRED_TYPE_GENERIC,
            "TargetName": TARGET,
            "UserName": _USERNAME,
            "CredentialBlob": token,
            "Persist": win32cred.CRED_PERSIST_LOCAL_MACHINE,
        }, 0)
    except Exception as exc:
        raise TokenUnavailable(f"could not store the bridge token: {exc}") from exc


def get_or_create_token() -> str:
    """The token, generating and storing one on first use.

    Raises TokenUnavailable rather than returning a weak or temporary value:
    a caller that cannot authenticate must refuse to serve, not serve openly.
    """
    existing = read_token()
    if existing:
        return existing
    token = secrets.token_hex(_TOKEN_BYTES)
    write_token(token)
    # Read back rather than trusting the write, so a silently-failed store
    # cannot leave the two sides holding different secrets.
    stored = read_token()
    if not stored:
        raise TokenUnavailable("the bridge token did not persist")
    return stored


def delete_token() -> bool:
    """Remove the credential. True when one was deleted."""
    win32cred = _win32cred()
    try:
        win32cred.CredDelete(TARGET, win32cred.CRED_TYPE_GENERIC, 0)
        return True
    except Exception:
        return False
