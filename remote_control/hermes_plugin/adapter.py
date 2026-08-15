"""GatedWhatsAppAdapter — strict admission control for self-chat.

Bhuvan's WhatsApp self-chat is his notes app. Hermes' bundled adapter processes
every DM that passes its allowlist (`whatsapp_common.py`, DM branch: "DMs that
pass the policy gate are always processed"). That is correct for a normal bot
and wrong for a notes chat: it would send every grocery list to a language
model.

This adapter overrides `_should_process_message` to require, in order:

  1. the message comes from the one authorized sender, and
  2. the message *begins* with an assistant name.

Both checks are deterministic code in Bhuvan's own project, deliberately
outside this Hermes installation, so upgrading Hermes cannot silently remove
them. The gate logic imports nothing beyond the standard library, so it loads
cleanly inside the Hermes virtualenv despite the two projects having
incompatible dependency trees.

Failure to load the gate disables WhatsApp entirely. That is intentional: the
only safe failure mode for admission control is "nothing gets through".
"""

import asyncio
import importlib.util
import logging
import os
import sys
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

#: Where Bhuvan's project lives. Overridable so the path is not baked in.
_PROJECT_ROOT = Path(
    os.environ.get("NEXUS_PROJECT_ROOT", r"D:\ai model")
).resolve()

_GATE_PACKAGE = "nexus_admission"


def _load_gate():
    """Import the admission controller from the project, by explicit path.

    Loaded under a private module name rather than by putting the project on
    `sys.path`: Hermes must not gain the ability to import the rest of the
    assistant codebase, and the assistant's module names must not shadow
    anything here.
    """
    if _GATE_PACKAGE in sys.modules:
        return sys.modules[_GATE_PACKAGE]

    security_dir = _PROJECT_ROOT / "remote_control" / "security"
    init_file = security_dir / "__init__.py"
    if not init_file.exists():
        raise FileNotFoundError(f"admission gate not found at {security_dir}")

    spec = importlib.util.spec_from_file_location(
        _GATE_PACKAGE, init_file, submodule_search_locations=[str(security_dir)]
    )
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load admission gate from {init_file}")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = _GATE_PACKAGE
    sys.modules[_GATE_PACKAGE] = module
    try:
        spec.loader.exec_module(module)
    except Exception:
        sys.modules.pop(_GATE_PACKAGE, None)
        raise
    return module


_gate = _load_gate()
AdmissionController = _gate.AdmissionController


def _load_bridge_client():
    """Import the NEXUS bridge client, again by explicit path.

    Same reasoning as the gate: the client is pure stdlib so it loads inside
    the Hermes virtualenv, and loading it by path keeps the rest of the
    assistant codebase unimportable from here.
    """
    name = "nexus_bridge_client"
    if name in sys.modules:
        return sys.modules[name]

    package = "nexus_remote_control"
    if package not in sys.modules:
        init_file = _PROJECT_ROOT / "remote_control" / "__init__.py"
        spec = importlib.util.spec_from_file_location(
            package, init_file,
            submodule_search_locations=[str(_PROJECT_ROOT / "remote_control")])
        module = importlib.util.module_from_spec(spec)
        sys.modules[package] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(package, None)
            raise

    spec = importlib.util.spec_from_file_location(
        f"{package}.bridge_client",
        _PROJECT_ROOT / "remote_control" / "bridge_client.py")
    module = importlib.util.module_from_spec(spec)
    module.__package__ = package
    sys.modules[f"{package}.bridge_client"] = module
    spec.loader.exec_module(module)
    sys.modules[name] = module
    return module


from plugins.platforms.whatsapp.adapter import (  # noqa: E402
    WhatsAppAdapter,
    _is_connected,
    _apply_yaml_config,
    _standalone_send,
    check_whatsapp_requirements,
    interactive_setup,
)


class GatedWhatsAppAdapter(WhatsAppAdapter):
    """The bundled adapter plus a pre-LLM admission gate."""

    def __init__(self, config):
        super().__init__(config)
        self._admission = AdmissionController()
        if not self._admission.configured:
            # No allowlist means every sender is unauthorized, so the adapter
            # would silently answer nobody. Say so once, loudly, at startup.
            logger.error(
                "NEXUS admission gate has no authorized senders configured; "
                "all WhatsApp messages will be ignored"
            )
        else:
            logger.info("NEXUS admission gate active")

    # ------------------------------------------------------------------
    # The gate. Synchronous, called as the first statement of
    # _build_message_event, before any MessageEvent exists.
    # ------------------------------------------------------------------
    def _should_process_message(self, data: Dict[str, Any]) -> bool:
        try:
            sender = data.get("senderId") or data.get("from") or data.get("chatId")
            body = data.get("body")

            # Media without a caption cannot carry a prefix, so it cannot be
            # addressed to an assistant. A photo saved to self-chat is a photo.
            admission = self._admission.admit(sender, body)
            if not admission.allowed:
                # `reason` is content-free by construction; the message text is
                # never logged, at any level.
                logger.debug("NEXUS gate: ignored (%s)", admission.reason)
                return False

            # Hand the routing decision downstream. `raw_message` is the same
            # dict the adapter passes through to MessageEvent, so Phase 4 can
            # read the target without re-parsing the prefix.
            data["nexusTarget"] = admission.target
            data["nexusCommand"] = admission.command
        except Exception:
            # ponytail: a broken gate must never mean an open gate.
            logger.exception("NEXUS gate failed; refusing the message")
            return False

        # Only now consult Hermes' own policy (broadcast chats, group rules,
        # allowlists). Ours is additive: it can deny, never permit.
        return super()._should_process_message(data)

    # ------------------------------------------------------------------
    # Delivery. An addressed message is answered by JARVIS/FRIDAY over the
    # NEXUS bridge, not by Hermes' own agent: Hermes is the WhatsApp
    # transport here, not a second assistant with its own model, memory, and
    # tools. Returning without calling super() means the Hermes agent loop
    # never sees the message at all.
    # ------------------------------------------------------------------
    async def handle_message(self, event) -> None:
        raw = getattr(event, "raw_message", None)
        target = (raw or {}).get("nexusTarget") if isinstance(raw, dict) else None
        if not target:
            # ponytail: no routing decision attached means the gate did not
            # admit this, or something rebuilt the event. Do not fall through
            # to the Hermes agent — that is what the gate exists to prevent.
            logger.debug("NEXUS: message without a routing target; dropping")
            return

        command = (raw.get("nexusCommand") or "").strip()
        chat_id = getattr(getattr(event, "source", None), "chat_id", "") or ""

        try:
            reply, attachment = await asyncio.to_thread(
                self._ask_assistant, target, command)
        except Exception:
            logger.exception("NEXUS: bridge call failed")
            reply, attachment = ("Something went wrong reaching the assistant "
                                 "on the laptop."), None
        if attachment:
            import base64
            try:
                await self._send_png(chat_id,
                                     base64.b64decode(attachment["b64"]),
                                     attachment.get("caption") or "")
                return
            except Exception:
                logger.exception("NEXUS: attachment delivery failed")
        if chat_id and reply:
            await self.send(chat_id, reply)

    def _ask_assistant(self, target: str, command: str):
        """Blocking bridge round-trip. Runs off the event loop.

        Returns (text, attachment) — an attachment rides home with its own
        reply rather than needing a second channel back to the phone.
        """
        client_module = _load_bridge_client()
        client = client_module.BridgeClient()
        response = client.ask(target, command)
        if response.ok:
            attachment = (response.data or {}).get("attachment")
            return response.text, attachment
        return (response.error or client_module.OFFLINE_MESSAGE), None

    async def _send_png(self, chat_id: str, data: bytes, caption: str) -> bool:
        """Deliver a screenshot, then remove the file we wrote to send it.

        The bridge needs a path, so the image touches disk for the length of
        one send and is removed in a finally block — the same rule the capture
        side follows.
        """
        import os
        import tempfile

        handle, path = tempfile.mkstemp(prefix="nexus_send_", suffix=".png")
        try:
            with os.fdopen(handle, "wb") as file:
                file.write(data)
            result = await self.send_image_file(chat_id, path, caption)
            return bool(getattr(result, "success", result))
        except Exception:
            logger.exception("NEXUS: image delivery failed")
            return False
        finally:
            try:
                os.remove(path)
            except OSError:
                logger.warning("NEXUS: could not remove the sent image")


def _build_adapter(config):
    return GatedWhatsAppAdapter(config)


def register(ctx) -> None:
    """Plugin entry point. Registers under the same platform name as the
    bundled adapter, which the registry treats as last-writer-wins."""
    ctx.register_platform(
        name="whatsapp",
        label="WhatsApp",
        adapter_factory=_build_adapter,
        check_fn=check_whatsapp_requirements,
        is_connected=_is_connected,
        required_env=["WHATSAPP_ENABLED"],
        install_hint="WhatsApp requires a Node.js bridge — see the WhatsApp messaging docs",
        setup_fn=interactive_setup,
        apply_yaml_config_fn=_apply_yaml_config,
        allowed_users_env="WHATSAPP_ALLOWED_USERS",
        allow_all_env="WHATSAPP_ALLOW_ALL_USERS",
        cron_deliver_env_var="WHATSAPP_HOME_CHANNEL",
        standalone_sender_fn=_standalone_send,
        max_message_length=4096,
        emoji="💬",
        allow_update_command=True,
    )
