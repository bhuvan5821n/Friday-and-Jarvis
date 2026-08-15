# Rejected FRIDAY UI prototypes

These are the earlier FRIDAY interface attempts, kept for reference. They are
**not imported** by the application.

## Why they were rejected

- A procedurally painted face (oval head, drawn eyes, line mouth) instead of
  FRIDAY's real video identity.
- Separate Normal and Battle dashboards — two interfaces where there should be
  one state-driven interface.
- Terminal typography and thin cyan wireframe panels throughout.
- Placeholder system and AI values that were not read from the backend.

## Where the code still lives

`ReferenceDeck` and `FaceCanvas` remain defined in `ui.py` but are no longer
constructed for FRIDAY:

- `ReferenceDeck` (ui.py) — unreferenced; the FRIDAY home page now mounts
  `friday.window.FridayInterface`.
- `FaceCanvas` (ui.py) — still reachable only if FRIDAY's new interface is
  absent; JARVIS uses `HudCanvas` and is unaffected.

They were left in place rather than deleted because `ui.py` is a large shared
file that also hosts JARVIS; excising them is a separate, riskier change and
buys nothing while they are inert.

## Replacement

`friday/` — theme, widgets, data, panels, avatar, states, bridge, window.
