"""JARVIS AI Studio plugin platform.

The package has no PyQt dependency.  UI pages are adapters over these stable
contracts, letting new plugins be installed without changing JARVIS core.
"""
from .contracts import StudioManifest, StudioRequest, StudioResult
from .registry import StudioRegistry, registry
from .router import StudioIntentRouter, StudioRoute

__all__ = [
    "StudioManifest", "StudioRequest", "StudioResult", "StudioRegistry",
    "StudioIntentRouter", "StudioRoute", "registry",
]
