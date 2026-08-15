"""NEXUS remote control — the shared, assistant-neutral remote gateway.

NEXUS is not a third assistant. It is the boundary layer that lets JARVIS and
FRIDAY be reached from WhatsApp. It owns admission control (who may speak, and
whether a message was addressed to an assistant at all), the local IPC bridge,
and the confirmation state machine. Personality, voice, and UI stay entirely in
the existing assistants.

Everything under `remote_control.security` is deliberately pure-stdlib: it is
imported by the Hermes plugin, which runs in a *separate* Python environment
with conflicting dependencies. Nothing here may import PyQt6, the model router,
or any project module that pulls those in.
"""
