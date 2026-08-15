"""FRIDAY's design tokens.

One place defines colour, type, spacing and radius so panels cannot drift apart
visually.  Values are deliberately restrained: amber and crimson are reserved
for real warnings and errors, so a coloured interface always means something.
"""
from __future__ import annotations

# ---- colour --------------------------------------------------------------

BG_DEEP = "#04070f"        # window base — deep navy-black
BG_PANEL = "rgba(11, 21, 38, 210)"
BG_PANEL_SOLID = "#0b1526"
BG_ELEVATED = "rgba(17, 31, 52, 235)"
BG_INPUT = "rgba(8, 17, 31, 235)"

BORDER = "rgba(72, 138, 190, 90)"
BORDER_STRONG = "rgba(0, 212, 255, 130)"
BORDER_SUBTLE = "rgba(64, 122, 168, 45)"

CYAN = "#00d4ff"
BLUE = "#3b82f6"
VIOLET = "#8b5cf6"
GREEN = "#22c55e"
AMBER = "#ffb020"
CRIMSON = "#ef4444"

TEXT = "#dceaf7"           # primary copy
TEXT_DIM = "#8fa9c2"       # secondary copy
TEXT_FAINT = "#54708c"     # labels, disabled
TEXT_MUTED = "#3f5670"

# ---- type ----------------------------------------------------------------

#: Modern UI face for everything a person reads as language.
FONT_UI = "Segoe UI Variable Display, Segoe UI, Inter, sans-serif"
#: Monospace strictly for logs, code and technical numerics.
FONT_MONO = "Cascadia Mono, Consolas, monospace"

FS_HERO = 22
FS_TITLE = 13
FS_BODY = 11
FS_LABEL = 9
FS_MICRO = 8

# ---- metrics -------------------------------------------------------------

RADIUS = 10
RADIUS_SM = 7
GAP = 12
PAD = 14

COL_LEFT = 268
COL_RIGHT = 300
DOCK_H = 92
TOPBAR_H = 60


def panel_qss(border: str = BORDER, bg: str = BG_PANEL) -> str:
    """Shared look for a glass panel surface."""
    return (f"background:{bg}; border:1px solid {border};"
            f"border-radius:{RADIUS}px;")
