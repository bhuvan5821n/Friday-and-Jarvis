"""Registry for built-in and future installed AI Studio plugins."""
from __future__ import annotations

from collections.abc import Iterable
import threading

from .contracts import StudioManifest, StudioPlugin


class StudioRegistry:
    def __init__(self) -> None:
        self._plugins: dict[str, StudioPlugin] = {}
        self._lock = threading.RLock()

    def register(self, plugin: StudioPlugin, *, replace: bool = False) -> None:
        manifest = plugin.manifest
        with self._lock:
            if manifest.id in self._plugins and not replace:
                raise ValueError(f"studio already registered: {manifest.id}")
            self._plugins[manifest.id] = plugin

    def unregister(self, studio_id: str) -> StudioPlugin | None:
        with self._lock:
            return self._plugins.pop(studio_id, None)

    def get(self, studio_id: str) -> StudioPlugin | None:
        with self._lock:
            return self._plugins.get(studio_id)

    def manifests(self) -> tuple[StudioManifest, ...]:
        with self._lock:
            return tuple(plugin.manifest for plugin in self._plugins.values())

    def __iter__(self) -> Iterable[StudioPlugin]:
        with self._lock:
            return iter(tuple(self._plugins.values()))


registry = StudioRegistry()
