"""Stable contracts shared by first-party and external AI Studio plugins."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import uuid4


InputKind = Literal[
    "text", "voice", "image", "video", "audio", "pdf", "document",
    "spreadsheet", "archive", "code", "folder", "screen", "unknown",
]


@dataclass(frozen=True, slots=True)
class Attachment:
    path: Path
    kind: InputKind
    size_bytes: int = 0
    media_type: str | None = None


@dataclass(frozen=True, slots=True)
class StudioManifest:
    id: str
    name: str
    version: str
    capabilities: tuple[str, ...]
    accepted_inputs: tuple[InputKind, ...] = ("text",)
    description: str = ""
    ui_entrypoint: str | None = None

    def __post_init__(self) -> None:
        if not self.id or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in self.id):
            raise ValueError("studio id must use lowercase letters, numbers, _ or -")
        if not self.name or not self.version:
            raise ValueError("studio name and version are required")


@dataclass(slots=True)
class StudioRequest:
    prompt: str = ""
    attachments: list[Attachment] = field(default_factory=list)
    project_id: str | None = None
    conversation_id: str | None = None
    model_override: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    request_id: str = field(default_factory=lambda: uuid4().hex)


@dataclass(slots=True)
class StudioResult:
    status: Literal["accepted", "streaming", "completed", "failed"]
    message: str = ""
    artifacts: list[Path] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class StudioPlugin(Protocol):
    manifest: StudioManifest

    def handle(self, request: StudioRequest) -> StudioResult: ...
