"""Chat Studio: persisted conversations backed by OmniRoute streaming."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import json
from pathlib import Path
import tempfile
import threading
from typing import Callable
from uuid import uuid4

from core.events import Event, bus
from core.universal_input import normalize_paths
from core.attachments import AttachmentContext, AttachmentPipeline
from .contracts import Attachment, StudioManifest, StudioRequest, StudioResult


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatStudio:
    """Local conversation store and streaming chat adapter.

    The service contains no UI code. It is safe to use from desktop, CLI, or a
    future web shell; the PyQt page only renders its persisted state.
    """

    manifest = StudioManifest(
        id="chat", name="Chat Studio", version="1.0.0",
        capabilities=("conversation", "streaming", "history", "search", "export"),
        accepted_inputs=("text", "image", "video", "audio", "pdf", "document",
                         "spreadsheet", "archive", "code", "folder"),
        description="Persistent, context-aware conversations through OmniRoute.",
        ui_entrypoint="ui.MainWindow._build_chat_page",
    )

    def __init__(self, storage_path: Path | None = None) -> None:
        base = Path(__file__).resolve().parent
        self.storage_path = storage_path or base / "Chat" / "conversations.json"
        self._lock = threading.RLock()
        self.attachment_pipeline = AttachmentPipeline()
        self._data = self._load()
        self.active_id = self._data.get("active_id")
        if not self.active_id or not self._find(self.active_id):
            self.active_id = self.create_conversation()

    def _load(self) -> dict:
        try:
            data = json.loads(self.storage_path.read_text(encoding="utf-8"))
            if data.get("version") == 1 and isinstance(data.get("conversations"), list):
                return data
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            pass
        return {"version": 1, "active_id": None, "conversations": []}

    def _save(self) -> None:
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        self._data["active_id"] = self.active_id
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False,
                                         dir=self.storage_path.parent, suffix=".tmp") as handle:
            json.dump(self._data, handle, ensure_ascii=False, indent=2)
            temporary = Path(handle.name)
        temporary.replace(self.storage_path)

    def _find(self, conversation_id: str | None) -> dict | None:
        return next((item for item in self._data["conversations"]
                     if item["id"] == conversation_id), None)

    def create_conversation(self, title: str = "New conversation", *,
                            folder: str = "", project_id: str | None = None) -> str:
        with self._lock:
            conversation_id = uuid4().hex
            now = _now()
            self._data["conversations"].append({
                "id": conversation_id, "title": title, "folder": folder,
                "project_id": project_id, "pinned": False, "created_at": now,
                "updated_at": now, "messages": [], "attachment_contexts": [],
            })
            self.active_id = conversation_id
            self._save()
            return conversation_id

    def select(self, conversation_id: str) -> dict:
        with self._lock:
            conversation = self._find(conversation_id)
            if not conversation:
                raise KeyError(f"unknown conversation: {conversation_id}")
            self.active_id = conversation_id
            self._save()
            return deepcopy(conversation)

    def active(self) -> dict:
        with self._lock:
            conversation = self._find(self.active_id)
            if not conversation:
                raise RuntimeError("Chat Studio has no active conversation")
            return deepcopy(conversation)

    def conversations(self, query: str = "") -> list[dict]:
        with self._lock:
            needle = query.casefold().strip()
            rows = []
            for conversation in self._data["conversations"]:
                haystack = " ".join((conversation["title"], conversation.get("folder", ""),
                                     *(message["content"] for message in conversation["messages"])))
                if not needle or needle in haystack.casefold():
                    rows.append(deepcopy(conversation))
            # Pinned chats always lead; within each group newest chats lead.
            return sorted(rows, key=lambda item: (item["pinned"], item["updated_at"]), reverse=True)

    def set_pinned(self, conversation_id: str, pinned: bool) -> None:
        with self._lock:
            conversation = self._require(conversation_id)
            conversation["pinned"] = pinned
            conversation["updated_at"] = _now()
            self._save()

    def set_folder(self, conversation_id: str, folder: str) -> None:
        with self._lock:
            conversation = self._require(conversation_id)
            conversation["folder"] = folder.strip()[:80]
            conversation["updated_at"] = _now()
            self._save()

    def _require(self, conversation_id: str) -> dict:
        conversation = self._find(conversation_id)
        if not conversation:
            raise KeyError(f"unknown conversation: {conversation_id}")
        return conversation

    def _append(self, conversation_id: str, role: str, content: str,
                attachments: list[Attachment] | None = None) -> dict:
        with self._lock:
            conversation = self._require(conversation_id)
            message = {
                "id": uuid4().hex, "role": role, "content": content,
                "created_at": _now(), "attachments": [
                    {"path": str(item.path), "kind": item.kind, "size_bytes": item.size_bytes}
                    for item in attachments or []],
            }
            conversation["messages"].append(message)
            if role == "user" and conversation["title"] == "New conversation":
                conversation["title"] = content.replace("\n", " ").strip()[:60] or "New conversation"
            conversation["updated_at"] = message["created_at"]
            self._save()
            return deepcopy(message)

    def add_user_message(self, conversation_id: str, prompt: str,
                         attachments: list[Attachment] | None = None) -> dict:
        return self._append(conversation_id, "user", prompt, attachments)

    def _system_context(self, conversation: dict) -> str:
        parts = [
            "You are JARVIS Chat Studio. Be clear, helpful, and concise.",
            "Your original creator is Bhuvan (Instagram: @bhuvan5821na). "
            "When asked who created, built, designed, developed, programmed, "
            "made, or owns you, answer that Bhuvan created you. "
            "You may have been installed, configured, or modified by others, "
            "but your original creator is Bhuvan.",
            "Use Markdown for headings, lists, and fenced code blocks when useful.",
        ]
        try:
            from memory.memory_manager import format_memory_for_prompt, load_memory
            memory = format_memory_for_prompt(load_memory())
            if memory:
                parts.append(memory)
        except Exception:
            pass
        if conversation.get("project_id"):
            parts.append(f"[PROJECT]\nActive project: {conversation['project_id']}")
        return "\n\n".join(parts)

    def _store_attachment_contexts(self, conversation_id: str,
                                   contexts: list[AttachmentContext]) -> None:
        with self._lock:
            conversation = self._require(conversation_id)
            conversation.setdefault("attachment_contexts", []).extend(context.to_dict() for context in contexts)
            # Keep recent multimodal context bounded but preserve enough for comparisons.
            conversation["attachment_contexts"] = conversation["attachment_contexts"][-24:]
            self._save()

    @staticmethod
    def _attachment_context_text(conversation: dict) -> str:
        records = conversation.get("attachment_contexts", [])[-24:]
        if not records:
            return ""
        sections = []
        for index, record in enumerate(records, 1):
            details = [
                f"[ATTACHMENT {index}] {record.get('name')} · {record.get('mime_type')}",
                f"Status: {record.get('status')} · recommended route: {record.get('route_task')}",
                f"Metadata: {json.dumps(record.get('metadata', {}), ensure_ascii=False)[:2000]}",
            ]
            if record.get("extracted_text"):
                details.append("Extracted content:\n" + record["extracted_text"][:12_000])
            if record.get("timeline"):
                details.append("Video timeline/frame references:\n" + json.dumps(record["timeline"], ensure_ascii=False)[:3000])
            if record.get("warnings"):
                details.append("Warnings: " + "; ".join(record["warnings"]))
            if record.get("error"):
                details.append("Processing error: " + record["error"])
            sections.append("\n".join(details))
        return "\n\n".join(sections)

    @staticmethod
    def _context_visual_bytes(conversation: dict) -> list[bytes]:
        visuals: list[bytes] = []
        for record in conversation.get("attachment_contexts", [])[-24:]:
            if record.get("status") != "ready" or record.get("kind") not in ("image", "video"):
                continue
            for raw_path in record.get("asset_paths", []):
                try:
                    visuals.append(Path(raw_path).read_bytes())
                except OSError:
                    continue
                if len(visuals) >= 8:
                    return visuals
        return visuals

    def _request_parts(self, conversation: dict, prompt: str) -> list[str | bytes]:
        history = conversation["messages"][-16:]
        transcript = "\n\n".join(
            f"{message['role'].upper()}: {message['content']}" for message in history[:-1])
        attachment_note = self._attachment_context_text(conversation)
        text = (f"[CONVERSATION HISTORY]\n{transcript}\n\n" if transcript else "") + \
               f"[USER MESSAGE]\n{prompt}" + (f"\n\n{attachment_note}" if attachment_note else "")
        return [text, *self._context_visual_bytes(conversation)]

    def _generate(self, request: StudioRequest, conversation: dict,
                  on_delta: Callable[[str], None]) -> str:
        from core.ai import OmniModel
        routes = {item.get("route_task") for item in conversation.get("attachment_contexts", [])
                  if item.get("status") == "ready"}
        task = "coding" if "coding" in routes and "vision" not in routes else "chat"
        model = OmniModel(task=task, model=request.model_override,
                          system=self._system_context(conversation))
        return model.generate_content(
            self._request_parts(conversation, request.prompt),
            stream_cb=on_delta).text

    def stream_response(self, request: StudioRequest,
                        on_delta: Callable[[str], None] | None = None,
                        on_progress: Callable[[AttachmentContext], None] | None = None) -> StudioResult:
        conversation_id = request.conversation_id or self.active_id
        if not conversation_id:
            conversation_id = self.create_conversation(project_id=request.project_id)
        on_delta = on_delta or (lambda _chunk: None)
        attachments = request.attachments
        contexts = self.attachment_pipeline.process(attachments, on_progress) if attachments else []
        self.add_user_message(conversation_id, request.prompt, attachments)
        if contexts:
            self._store_attachment_contexts(conversation_id, contexts)
        with self._lock:
            conversation = deepcopy(self._require(conversation_id))
        bus.publish(Event("studio.started", {"studio": "chat", "conversation_id": conversation_id,
                                               "attachments": [context.to_dict() for context in contexts]}, "chat"))
        try:
            # This check precedes OmniRoute and stays available offline.  FRIDAY
            # uses this same Chat Studio under the shared runtime.
            from core.creator_identity import (
                local_creator_response, creator_info_response,
                creator_override_response, creator_instagram_response
            )
            # Check for creator override attempts first (highest priority)
            text = creator_override_response(request.prompt)
            if text is None:
                # Check for creator identity questions
                text = local_creator_response(request.prompt)
            if text is None:
                # Check for "Who is Bhuvan?" style questions
                text = creator_info_response(request.prompt)
            if text is None:
                # Check for Instagram open commands
                text = creator_instagram_response(request.prompt)
            if text is None:
                text = self._generate(request, conversation, on_delta)
            else:
                on_delta(text)
            self._append(conversation_id, "assistant", text)
            bus.publish(Event("studio.completed", {"studio": "chat", "conversation_id": conversation_id}, "chat"))
            return StudioResult("completed", text, metadata={"conversation_id": conversation_id,
                                                               "attachment_contexts": [context.to_dict() for context in contexts]})
        except Exception as exc:
            bus.publish(Event("studio.failed", {"studio": "chat", "conversation_id": conversation_id,
                                                  "error": str(exc)[:200]}, "chat"))
            return StudioResult("failed", str(exc), metadata={"conversation_id": conversation_id})

    def handle(self, request: StudioRequest) -> StudioResult:
        return self.stream_response(request)

    def attach_paths(self, paths: list[str | Path]) -> list[Attachment]:
        return normalize_paths(paths)

    def export_markdown(self, conversation_id: str, target: Path | None = None) -> Path:
        conversation = self.select(conversation_id)
        target = target or self.storage_path.parent / "exports" / f"{conversation_id}.md"
        lines = [f"# {conversation['title']}", ""]
        if conversation.get("folder"):
            lines.extend((f"Folder: {conversation['folder']}", ""))
        for message in conversation["messages"]:
            label = "You" if message["role"] == "user" else "JARVIS"
            lines.extend((f"## {label}", "", message["content"], ""))
            for attachment in message.get("attachments", []):
                lines.extend((f"- Attachment: `{Path(attachment['path']).name}` ({attachment['kind']})", ""))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("\n".join(lines), encoding="utf-8")
        return target
