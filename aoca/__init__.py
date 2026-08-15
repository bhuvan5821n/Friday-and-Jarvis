"""ASTRAEUS OMEGA COGNITIVE ARCHITECTURE — foundation layers.

Phases 1-3 only: safety kernel, event/trace transport, verified execution.
No graph, no learning, no world model, no planner. Those flags exist in
`aoca.config` and are hard-wired off.

Nothing here imports torch, numpy, or PyQt. Importing `aoca` must stay cheap
enough to sit on the assistant's startup path.
"""
from __future__ import annotations

__version__ = "0.3.0"

from aoca.config import flags

__all__ = ["flags", "__version__"]
