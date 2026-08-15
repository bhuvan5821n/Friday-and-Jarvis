"""Repository-relative resolution of FRIDAY's avatar media.

Paths are resolved from this file's location, never from the process working
directory, so the avatar renders identically whether FRIDAY is launched from
the tray, a shortcut, a service, or a terminal in another folder.

A missing clip is never a silent failure: it is logged once and reported to the
caller so the UI can fall back to neutral and say so honestly.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

log = logging.getLogger("friday.assets")

REPO_ROOT = Path(__file__).resolve().parent.parent
AVATAR_DIR = REPO_ROOT / "assets" / "friday" / "avatar"
MANIFEST_PATH = AVATAR_DIR / "emotion_manifest.json"

#: Emotion used whenever a requested clip is unavailable.
FALLBACK_EMOTION = "neutral"


@dataclass(frozen=True)
class EmotionClip:
    """One playable avatar state."""

    name: str
    path: Path
    loop: bool
    label: str

    @property
    def exists(self) -> bool:
        return self.path.is_file()


class AvatarLibrary:
    """The set of emotion clips FRIDAY can actually play.

    Availability is resolved once at load time; callers can therefore ask
    ``has(name)`` cheaply on every state change without touching the disk.
    """

    def __init__(self, manifest_path: Path = MANIFEST_PATH):
        self._manifest_path = manifest_path
        self._clips: dict[str, EmotionClip] = {}
        self._missing: list[str] = []
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self._manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            log.error("FRIDAY avatar manifest unreadable at %s: %s",
                      self._manifest_path, exc)
            return

        base = self._manifest_path.parent
        for name, spec in (raw.get("emotions") or {}).items():
            filename = spec.get("file")
            if not filename:
                log.warning("FRIDAY emotion %r has no 'file' entry; skipped", name)
                continue
            clip = EmotionClip(
                name=name,
                path=base / filename,
                loop=bool(spec.get("loop", True)),
                label=spec.get("label", name.replace("_", " ").title()),
            )
            if not clip.exists:
                self._missing.append(f"{name} -> {clip.path}")
                log.warning("FRIDAY avatar clip missing: %s (%s)", name, clip.path)
            self._clips[name] = clip

        if not self._clips:
            log.error("FRIDAY avatar library is empty; the avatar cannot render")

    # ---- queries -------------------------------------------------------

    def has(self, name: str) -> bool:
        clip = self._clips.get(name)
        return bool(clip and clip.exists)

    def get(self, name: str) -> EmotionClip | None:
        """Return the requested clip, or the fallback when it is unusable.

        Returns ``None`` only when even the fallback is missing, which the
        caller must surface rather than paper over.
        """
        clip = self._clips.get(name)
        if clip is not None and clip.exists:
            return clip
        if name != FALLBACK_EMOTION:
            log.info("FRIDAY emotion %r unavailable; falling back to %r",
                     name, FALLBACK_EMOTION)
            fallback = self._clips.get(FALLBACK_EMOTION)
            if fallback is not None and fallback.exists:
                return fallback
        return None

    @property
    def names(self) -> list[str]:
        return list(self._clips)

    @property
    def available(self) -> list[str]:
        return [n for n, c in self._clips.items() if c.exists]

    @property
    def missing(self) -> list[str]:
        """Human-readable 'emotion -> expected path' entries, for reporting."""
        return list(self._missing)

    def __len__(self) -> int:
        return len(self._clips)


@lru_cache(maxsize=1)
def library() -> AvatarLibrary:
    """Process-wide avatar library (the manifest is read exactly once)."""
    return AvatarLibrary()
