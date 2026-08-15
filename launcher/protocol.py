"""Lifecycle protocol shared by the launcher and the assistant.

One place for the command vocabulary, the IPC framing and the paths, so the
launcher and the running assistant cannot drift apart.

Security posture: the socket binds to 127.0.0.1 only, every request must carry
the per-install token from ``runtime/lifecycle_token``, and only the exact
command names in :data:`COMMANDS` are accepted. No command carries a shell
string — the payload is a small validated JSON object.
"""
from __future__ import annotations

import json
import os
import secrets
import socket
import sys
from pathlib import Path

def _find_base_dir() -> Path:
    """Repo root, whether running from source or frozen by PyInstaller.

    A frozen launcher may live in a subfolder (dist/launcher). Walk upward
    until the directory that actually contains the project (main.py or the
    runtime/ token dir) is found, so every entry point agrees on where the
    token and the registry live — otherwise IPC authentication breaks.
    """
    if getattr(sys, "frozen", False):
        here = Path(sys.executable).parent
    else:
        return Path(__file__).resolve().parent.parent
    for candidate in (here, *here.parents):
        # main.py is the definitive marker; a runtime/ dir alone could be a
        # stale one created next to the exe by an older build.
        if (candidate / "main.py").exists():
            return candidate
    return here


#: Repo root, shared by the launcher and the assistant.
BASE_DIR = _find_base_dir()

RUNTIME_DIR = BASE_DIR / "runtime"
REGISTRY_PATH = RUNTIME_DIR / "process_registry.json"
TOKEN_PATH = RUNTIME_DIR / "lifecycle_token"
CONFIG_PATH = BASE_DIR / "launcher" / "launcher_config.json"
LOG_DIR = BASE_DIR / "Logs"

#: The assistant's existing single-instance / show channel.
IPC_PORT = 48757
#: SpeechCore.exe's own channel (core/speechcore_bridge.py).
SPEECHCORE_PORT = 48800

#: Every lifecycle command the assistant will honour. Anything else is refused,
#: so a stray client cannot ask the app to do something arbitrary.
COMMANDS = frozenset({
    "PING",            # health probe; replies with the lifecycle state
    "SHOW_WINDOW",     # show the window if hidden
    "RESTORE_WINDOW",  # un-minimise
    "FOCUS_INPUT",     # put the caret in the prompt
    "SET_PERSONA",     # switch identity: {"persona": "jarvis"|"friday"}
    "OPEN_MIC",        # compact overlay + unmute + listen
    "CLOSE_MIC",       # mute, release the device, hide the overlay
    "SHUTDOWN",        # graceful full shutdown of this process tree
    "STATUS",          # readiness + registry summary
})

#: Readiness states reported over IPC. The launcher waits for READY.
STATES = ("STARTING", "INITIALIZING_BACKEND", "LOADING_MODELS",
          "CONNECTING_SERVICES", "READY", "FAILED", "SHUTTING_DOWN", "STOPPED")

DEFAULT_CONFIG = {
    "default_assistant": "friday",
    "remember_last_assistant": True,
    "show_console": False,
    "startup_timeout_seconds": 20,
    "shutdown_timeout_seconds": 8,
    "mic_overlay_enabled": True,
    "shortcut_keys": {
        "jarvis": "Ctrl+Alt+J",
        "friday": "Ctrl+Alt+F",
        "mic_open": "Ctrl+Alt+M",
        "mic_close": "Ctrl+Alt+Shift+M",
        "shutdown_all": "Ctrl+Alt+Shift+X",
    },
}


def load_config() -> dict:
    """Launcher config, falling back to defaults for anything missing.

    A corrupted file must not stop a shortcut from working, so unreadable
    JSON degrades to the defaults rather than raising.
    """
    cfg = dict(DEFAULT_CONFIG)
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            keys = dict(DEFAULT_CONFIG["shortcut_keys"])
            keys.update(raw.get("shortcut_keys") or {})
            cfg.update(raw)
            cfg["shortcut_keys"] = keys
    except FileNotFoundError:
        pass
    except Exception:
        pass  # corrupt config → defaults; the caller logs it
    return cfg


def save_config(cfg: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(CONFIG_PATH, json.dumps(cfg, indent=2))


def _atomic_write(path: Path, text: str) -> None:
    """Write via a temp file + replace, so a crash cannot leave a half file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".tmp{os.getpid()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---- token ---------------------------------------------------------------

def get_token() -> str:
    """The per-install shared secret, created on first use.

    Callers on this machine can read it; that is the intended trust boundary —
    it stops a remote or unprivileged client from issuing SHUTDOWN, not the
    logged-in user from controlling their own assistant.
    """
    try:
        token = TOKEN_PATH.read_text(encoding="utf-8").strip()
        if token:
            return token
    except Exception:
        pass
    token = secrets.token_hex(16)
    _atomic_write(TOKEN_PATH, token)
    if os.name == "nt":
        # Best-effort: restrict to the current user.
        try:
            import subprocess
            subprocess.run(
                ["icacls", str(TOKEN_PATH), "/inheritance:r",
                 "/grant:r", f"{os.environ.get('USERNAME', '')}:R"],
                capture_output=True,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        except Exception:
            pass
    return token


# ---- wire format ---------------------------------------------------------

def encode(command: str, token: str, **payload) -> bytes:
    """Frame one command as a single newline-terminated JSON object."""
    if command not in COMMANDS:
        raise ValueError(f"refusing to send unknown command {command!r}")
    body = {"command": command, "token": token}
    body.update(payload)
    return (json.dumps(body) + "\n").encode("utf-8")


def decode(raw: bytes, token: str) -> dict | None:
    """Parse and authenticate one request. ``None`` means reject it.

    Legacy note: older builds sent the bare string ``SHOW``. That is still
    accepted so an old process can be told to show itself, but it is the only
    unauthenticated command and it cannot shut anything down.
    """
    text = (raw or b"").decode("utf-8", "replace").strip()
    if not text:
        return None
    if text == "SHOW":
        return {"command": "SHOW_WINDOW", "legacy": True}
    try:
        body = json.loads(text)
    except Exception:
        return None
    if not isinstance(body, dict):
        return None
    if body.get("command") not in COMMANDS:
        return None
    if not secrets.compare_digest(str(body.get("token", "")), token):
        return None
    return body


def send(command: str, timeout: float = 2.0, port: int = IPC_PORT,
         **payload) -> dict | None:
    """Send one command to a running assistant and return its reply.

    ``None`` means nothing answered — the caller decides whether that means
    "cold start" or "already gone", which differs per command.
    """
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout) as c:
            c.sendall(encode(command, get_token(), **payload))
            c.settimeout(timeout)
            chunks = []
            while True:
                part = c.recv(4096)
                if not part:
                    break
                chunks.append(part)
                if b"\n" in part:
                    break
        reply = b"".join(chunks).decode("utf-8", "replace").strip()
        return json.loads(reply) if reply else {}
    except Exception:
        return None


def instance_running(timeout: float = 1.0) -> bool:
    """True when something is listening on the assistant's channel."""
    try:
        with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=timeout):
            return True
    except Exception:
        return False
