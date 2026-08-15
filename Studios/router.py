"""Transparent natural-language routing from universal input to a Studio."""
from __future__ import annotations

from dataclasses import dataclass

from .contracts import Attachment, StudioRequest


@dataclass(frozen=True, slots=True)
class StudioRoute:
    studio_id: str
    reason: str
    confidence: int
    manual: bool = False


class StudioIntentRouter:
    """Deterministic first-pass router; model routing remains OmniRoute's job."""

    _phrases = (
        ("image", ("create a logo", "generate an image", "make an image", "poster", "upscale", "inpaint", "outpaint")),
        ("video", ("cinematic trailer", "generate a video", "create a video", "reel", "lip sync", "storyboard")),
        ("music", ("compose", "generate music", "write a song", "instrumental", "lo-fi", "lofi", "background music")),
        ("voice", ("text to speech", "narrate", "audiobook", "podcast voice", "clone this voice")),
        ("document", ("summarize this pdf", "create a pdf", "powerpoint", "spreadsheet", "invoice", "resume", "meeting notes")),
        ("code", ("build a react", "debug", "repository", "refactor", "write code", "typescript", "python")),
        ("research", ("deep research", "compare sources", "research report", "citations", "knowledge graph")),
        ("automation", ("automate", "schedule", "macro", "browser workflow", "send an email", "whatsapp")),
    )

    _attachment_routes = {
        "image": "image", "video": "video", "audio": "voice", "pdf": "document",
        "document": "document", "spreadsheet": "document", "code": "code", "folder": "code",
    }

    def route(self, request: StudioRequest, manual_studio: str | None = None) -> StudioRoute:
        if manual_studio:
            return StudioRoute(manual_studio, "manual studio selection", 100, True)
        prompt = request.prompt.lower()
        for studio_id, phrases in self._phrases:
            if any(phrase in prompt for phrase in phrases):
                return StudioRoute(studio_id, f"matched {studio_id} intent", 91)
        for attachment in request.attachments:
            studio_id = self._attachment_routes.get(attachment.kind)
            if studio_id:
                return StudioRoute(studio_id, f"{attachment.kind} attachment", 80)
        return StudioRoute("chat", "general conversation", 65)

    def route_attachments(self, prompt: str, attachments: list[Attachment]) -> StudioRoute:
        return self.route(StudioRequest(prompt=prompt, attachments=attachments))
