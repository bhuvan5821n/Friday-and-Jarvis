"""MIME-sniffed, extensible multimodal attachment pipeline for JARVIS.

File names provide a usability hint only; bytes and container contents decide
which processor receives a file. Every processor returns a serializable
context record so a conversation can reuse an upload on later turns.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
import csv
import io
import json
import mimetypes
from pathlib import Path
import re
import tempfile
import time
from typing import Callable, Protocol
from uuid import uuid4
import wave
import zipfile

from Studios.contracts import Attachment, InputKind

_TEXT_LIMIT = 60_000
_HEAD_LIMIT = 32_768


@dataclass(slots=True)
class AttachmentContext:
    id: str
    source_path: str
    name: str
    kind: InputKind
    mime_type: str
    size_bytes: int
    status: str = "ready"
    route_task: str = "chat"
    metadata: dict = field(default_factory=dict)
    extracted_text: str = ""
    timeline: list[dict] = field(default_factory=list)
    asset_paths: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _text_from_head(raw: bytes) -> str | None:
    if b"\x00" in raw:
        return None
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("latin-1", errors="ignore") if raw.count(b"\x00") == 0 else None


def _zip_kind(path: Path) -> tuple[InputKind, str]:
    try:
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
        if "[Content_Types].xml" in names:
            if any(name.startswith("word/") for name in names): return "document", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
            if any(name.startswith("ppt/") for name in names): return "document", "application/vnd.openxmlformats-officedocument.presentationml.presentation"
            if any(name.startswith("xl/") for name in names): return "spreadsheet", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        return "archive", "application/zip"
    except (OSError, zipfile.BadZipFile):
        return "unknown", "application/octet-stream"


def sniff_file(path: Path) -> tuple[InputKind, str, list[str]]:
    """Identify a file from bytes/container markers, never extension alone."""
    warnings: list[str] = []
    try:
        with path.open("rb") as handle:
            head = handle.read(_HEAD_LIMIT)
    except OSError as exc:
        return "unknown", "application/octet-stream", [f"Unable to read file: {exc}"]
    if head.startswith(b"\xff\xd8\xff"): return "image", "image/jpeg", warnings
    if head.startswith(b"\x89PNG\r\n\x1a\n"): return "image", "image/png", warnings
    if head.startswith((b"GIF87a", b"GIF89a")): return "image", "image/gif", warnings
    if head.startswith(b"BM"): return "image", "image/bmp", warnings
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP": return "image", "image/webp", warnings
    if head[:4] == b"RIFF" and head[8:12] == b"WAVE": return "audio", "audio/wav", warnings
    if head[:4] == b"RIFF" and head[8:12] == b"AVI ": return "video", "video/avi", warnings
    if head.startswith(b"fLaC"): return "audio", "audio/flac", warnings
    if head.startswith(b"OggS"): return "audio", "audio/ogg", warnings
    if head.startswith(b"ID3") or head[:2] == b"\xff\xfb": return "audio", "audio/mpeg", warnings
    if head.startswith(b"%PDF-"): return "pdf", "application/pdf", warnings
    if head.startswith(b"PK\x03\x04"): return (*_zip_kind(path), warnings)
    if head.startswith(b"\x1aE\xdf\xa3"): return "video", "video/webm", warnings
    if len(head) >= 12 and head[4:8] == b"ftyp":
        brand = head[8:12].lower()
        if brand in (b"heic", b"heix", b"mif1", b"msf1"): return "image", "image/heic", warnings
        return "video", "video/mp4", warnings
    text = _text_from_head(head)
    if text is not None:
        stripped = text.lstrip()
        if stripped.startswith("<svg") or "<svg" in stripped[:1024]: return "image", "image/svg+xml", warnings
        if stripped.startswith(("{", "[")):
            try:
                json.loads(stripped)
                return "code", "application/json", warnings
            except json.JSONDecodeError:
                pass
        if stripped.startswith("<?xml") or (stripped.startswith("<") and ">" in stripped[:400]): return "code", "application/xml", warnings
        code_signals = ("def ", "class ", "function ", "import ", "#include", "const ", "let ", "var ", "SELECT ", "<!DOCTYPE html", "print(")
        if any(signal in text[:8000] for signal in code_signals): return "code", "text/x-source", warnings
        if path.suffix.lower() in (".yaml", ".yml") and ":" in text[:1000]: return "code", "application/yaml", warnings
        return "document", mimetypes.guess_type(path.name)[0] or "text/plain", warnings
    return "unknown", mimetypes.guess_type(path.name)[0] or "application/octet-stream", warnings


class Processor(Protocol):
    kinds: tuple[InputKind, ...]
    def process(self, attachment: Attachment, kind: InputKind, mime_type: str) -> AttachmentContext: ...


def _base(attachment: Attachment, kind: InputKind, mime_type: str, route_task: str) -> AttachmentContext:
    return AttachmentContext(uuid4().hex, str(attachment.path), attachment.path.name, kind,
                             mime_type, attachment.size_bytes, route_task=route_task,
                             metadata={"suffix": attachment.path.suffix.lower()})


class ImageProcessor:
    kinds = ("image",)
    def process(self, attachment, kind, mime_type):
        context = _base(attachment, kind, mime_type, "vision")
        context.asset_paths.append(str(attachment.path))
        try:
            from PIL import Image
            with Image.open(attachment.path) as image:
                image.verify()
            with Image.open(attachment.path) as image:
                context.metadata.update({"width": image.width, "height": image.height,
                                         "mode": image.mode, "format": image.format})
        except Exception as exc:
            if mime_type != "image/svg+xml":
                context.status = "failed"; context.error = f"Image validation failed: {exc}"
            else:
                context.warnings.append("SVG is preserved for a vision-capable model; raster preview is unavailable.")
        return context


class VideoProcessor:
    kinds = ("video",)
    def process(self, attachment, kind, mime_type):
        context = _base(attachment, kind, mime_type, "vision")
        try:
            import cv2
            capture = cv2.VideoCapture(str(attachment.path))
            if not capture.isOpened(): raise ValueError("OpenCV cannot open this video")
            frames = int(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0)
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0)
            width = int(capture.get(cv2.CAP_PROP_FRAME_WIDTH) or 0)
            height = int(capture.get(cv2.CAP_PROP_FRAME_HEIGHT) or 0)
            duration = round(frames / fps, 2) if fps else None
            context.metadata.update({"frame_count": frames, "fps": fps, "width": width, "height": height, "duration_seconds": duration})
            out = Path(tempfile.gettempdir()) / "jarvis-attachment-frames" / context.id
            out.mkdir(parents=True, exist_ok=True)
            sample_count = min(8, max(1, frames))
            for index, frame_number in enumerate(sorted({int(i * max(frames - 1, 0) / max(sample_count - 1, 1)) for i in range(sample_count)})):
                capture.set(cv2.CAP_PROP_POS_FRAMES, frame_number)
                ok, frame = capture.read()
                if not ok: continue
                frame_path = out / f"frame_{index + 1:02d}.jpg"
                cv2.imwrite(str(frame_path), frame)
                timestamp = round(frame_number / fps, 2) if fps else None
                context.asset_paths.append(str(frame_path))
                context.timeline.append({"timestamp_seconds": timestamp, "frame_path": str(frame_path)})
            capture.release()
            if not context.timeline: context.warnings.append("No representative frames could be extracted.")
        except Exception as exc:
            context.status = "failed"; context.error = f"Video processing failed: {exc}"
        return context


def _xml_text(raw: bytes) -> str:
    return re.sub(r"\s+", " ", " ".join(re.findall(r">([^<>]+)<", raw.decode("utf-8", errors="ignore")))).strip()


class DocumentProcessor:
    kinds = ("pdf", "document", "spreadsheet")
    def process(self, attachment, kind, mime_type):
        context = _base(attachment, kind, mime_type, "chat")
        path = attachment.path
        try:
            if kind == "pdf":
                raw = path.read_bytes()
                context.metadata["pdf_version"] = raw[5:8].decode("ascii", errors="ignore")
                try:
                    from pypdf import PdfReader
                    reader = PdfReader(io.BytesIO(raw)); context.metadata["pages"] = len(reader.pages)
                    context.extracted_text = "\n\n".join(page.extract_text() or "" for page in reader.pages)[:_TEXT_LIMIT]
                except ImportError:
                    matches = re.findall(rb"\(([^()]{1,1500})\)\s*Tj", raw)
                    context.extracted_text = "\n".join(item.decode("latin-1", errors="ignore") for item in matches)[:_TEXT_LIMIT]
                    context.warnings.append("Install pypdf for full PDF text, table, and image extraction; basic embedded text was used.")
            elif kind == "spreadsheet" and path.suffix.lower() in (".csv", ".tsv"):
                delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
                with path.open("r", encoding="utf-8", errors="replace", newline="") as handle:
                    rows = list(csv.reader(handle, delimiter=delimiter))[:200]
                context.metadata.update({"rows_sampled": len(rows), "columns": len(rows[0]) if rows else 0})
                context.extracted_text = "\n".join(" | ".join(row) for row in rows)[:_TEXT_LIMIT]
            elif path.suffix.lower() in (".docx", ".pptx", ".xlsx"):
                with zipfile.ZipFile(path) as archive:
                    names = archive.namelist()
                    xml_names = ([name for name in names if name == "word/document.xml"] if path.suffix.lower() == ".docx" else
                                 sorted(name for name in names if name.startswith("ppt/slides/") and name.endswith(".xml")) if path.suffix.lower() == ".pptx" else
                                 sorted(name for name in names if name.startswith("xl/sharedStrings") or name.startswith("xl/worksheets/")))
                    chunks = [_xml_text(archive.read(name)) for name in xml_names]
                context.metadata["parts_extracted"] = len(xml_names)
                context.extracted_text = "\n\n".join(chunk for chunk in chunks if chunk)[:_TEXT_LIMIT]
            else:
                context.extracted_text = path.read_text(encoding="utf-8", errors="replace")[:_TEXT_LIMIT]
            if not context.extracted_text.strip(): context.warnings.append("No extractable text was found; ask a question and JARVIS will use available metadata.")
        except Exception as exc:
            context.status = "failed"; context.error = f"Document extraction failed: {exc}"
        return context


class AudioProcessor:
    kinds = ("audio",)
    def process(self, attachment, kind, mime_type):
        context = _base(attachment, kind, mime_type, "chat")
        try:
            if mime_type == "audio/wav":
                with wave.open(str(attachment.path), "rb") as audio:
                    rate, frames = audio.getframerate(), audio.getnframes()
                    context.metadata.update({"channels": audio.getnchannels(), "sample_rate": rate,
                                             "duration_seconds": round(frames / rate, 2) if rate else None})
            context.warnings.append("Audio is available in this conversation. Configure a transcription provider to enable spoken-content extraction.")
        except Exception as exc:
            context.status = "failed"; context.error = f"Audio metadata extraction failed: {exc}"
        return context


class CodeProcessor:
    kinds = ("code", "folder")
    def process(self, attachment, kind, mime_type):
        context = _base(attachment, kind, mime_type, "coding")
        try:
            if attachment.path.is_dir():
                files = [item for item in attachment.path.rglob("*") if item.is_file()][:200]
                context.metadata.update({"file_count_sampled": len(files)})
                context.extracted_text = "\n".join(str(item.relative_to(attachment.path)) for item in files)
            else:
                context.extracted_text = attachment.path.read_text(encoding="utf-8", errors="replace")[:_TEXT_LIMIT]
                context.metadata["lines"] = context.extracted_text.count("\n") + 1
                context.metadata["language_hint"] = attachment.path.suffix.lstrip(".").lower() or "source"
        except Exception as exc:
            context.status = "failed"; context.error = f"Code extraction failed: {exc}"
        return context


class ArchiveProcessor:
    kinds = ("archive",)
    def process(self, attachment, kind, mime_type):
        context = _base(attachment, kind, mime_type, "chat")
        try:
            with zipfile.ZipFile(attachment.path) as archive:
                infos = archive.infolist()[:300]
                total = sum(info.file_size for info in infos)
                context.metadata.update({"entries_sampled": len(infos), "uncompressed_bytes_sampled": total})
                context.extracted_text = "\n".join(f"{info.filename} ({info.file_size} bytes)" for info in infos)
                if total > 500 * 1024 * 1024: context.warnings.append("Archive content was listed but not extracted because its sampled uncompressed size is large.")
        except Exception as exc:
            context.status = "failed"; context.error = f"Archive inspection failed: {exc}"
        return context


class AttachmentPipeline:
    """Routes MIME-detected files to isolated processors without silent drops."""
    def __init__(self) -> None:
        processors = (ImageProcessor(), VideoProcessor(), DocumentProcessor(), AudioProcessor(), CodeProcessor(), ArchiveProcessor())
        self._processors = {kind: processor for processor in processors for kind in processor.kinds}

    def process(self, attachments: list[Attachment], progress: Callable[[AttachmentContext], None] | None = None) -> list[AttachmentContext]:
        contexts = []
        for attachment in attachments:
            kind, mime_type, warnings = sniff_file(attachment.path)
            processor = self._processors.get(kind)
            if not processor:
                context = _base(attachment, kind, mime_type, "chat")
                context.status = "failed"; context.error = "This file type could not be safely identified. Supported types: images, video, PDF/Office/text, audio, ZIP, and source code."
            else:
                context = processor.process(attachment, kind, mime_type)
            context.warnings.extend(warnings)
            contexts.append(context)
            if progress: progress(context)
        return contexts


def attachment_from_path(path: Path) -> Attachment:
    kind, mime_type, _ = sniff_file(path)
    return Attachment(path=path, kind=kind, size_bytes=path.stat().st_size, media_type=mime_type)
