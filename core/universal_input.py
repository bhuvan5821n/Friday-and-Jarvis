"""Universal-input normalization shared by every JARVIS Studio.

This module only identifies and validates local inputs.  Parsing and provider
uploads remain studio responsibilities, keeping sensitive file handling local
until a user starts a specific task.
"""
from __future__ import annotations

from pathlib import Path

from Studios.contracts import Attachment, InputKind
from core.attachments import attachment_from_path, sniff_file


_EXTENSIONS: dict[str, InputKind] = {
    **dict.fromkeys((".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".svg"), "image"),
    **dict.fromkeys((".mp4", ".mov", ".mkv", ".avi", ".webm"), "video"),
    **dict.fromkeys((".mp3", ".wav", ".m4a", ".aac", ".ogg", ".flac"), "audio"),
    ".pdf": "pdf",
    **dict.fromkeys((".doc", ".docx", ".ppt", ".pptx", ".txt", ".md", ".rtf"), "document"),
    **dict.fromkeys((".xls", ".xlsx", ".csv", ".tsv", ".ods"), "spreadsheet"),
    **dict.fromkeys((".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"), "archive"),
    **dict.fromkeys((".py", ".js", ".ts", ".jsx", ".tsx", ".html", ".css", ".java", ".c", ".cpp", ".cs", ".go", ".rs", ".rb", ".php", ".sql", ".sh"), "code"),
}


def classify_path(path: str | Path) -> InputKind:
    candidate = Path(path)
    if candidate.is_dir():
        return "folder"
    kind, _, _ = sniff_file(candidate)
    return kind


def normalize_paths(paths: list[str | Path], *, max_files: int = 20,
                    max_file_bytes: int = 250 * 1024 * 1024) -> list[Attachment]:
    """Validate existing local paths without reading their contents."""
    if len(paths) > max_files:
        raise ValueError(f"at most {max_files} inputs can be attached at once")
    attachments: list[Attachment] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(path)
        size = 0 if path.is_dir() else path.stat().st_size
        if size > max_file_bytes:
            raise ValueError(f"input exceeds {max_file_bytes // (1024 * 1024)} MB: {path.name}")
        attachments.append(Attachment(path=path, kind="folder", size_bytes=0) if path.is_dir()
                           else attachment_from_path(path))
    return attachments
