"""FRIDAY-specific presentation and state infrastructure.

Nothing in this package is imported by, or required for, the JARVIS visual
identity.  The shared application can opt into it only for persona='friday'.
"""

from .mode_manager import FridayModeManager
from .state_controller import FridayStateController

__all__ = ["FridayModeManager", "FridayStateController"]
