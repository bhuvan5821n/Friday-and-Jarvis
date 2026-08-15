"""Voice-tool bridge: one `web_intelligence` tool for both assistants.

Follows the existing action-module contract in this codebase: a synchronous
function taking `parameters` + `player`, returning a short string the LLM
speaks. The heavy lifting stays in WebIntelligenceService; this module only
translates between the tool-call schema and typed service calls, and adapts
progress events into UI log lines.

Persona affects narration style only — never the facts.
"""
from __future__ import annotations

import logging
import threading

from .models import WebQuery
from .providers import ProviderError
from .security import redact_secrets
from .service import WebIntelligenceService

log = logging.getLogger("webintel.tool")

_service: WebIntelligenceService | None = None
_service_lock = threading.Lock()

#: Progress events worth a UI log line (short names for the console).
_EVENT_LABELS = {
    "SEARCH_STARTED": "searching",
    "PAGE_READING": "reading",
    "COMPARISON_STARTED": "comparing sources",
    "INJECTION_DETECTED": "⚠ page contained agent-directed instructions (ignored)",
    "TASK_FAILED": "failed",
}


def get_service(player=None) -> WebIntelligenceService:
    """The one shared instance (JARVIS and FRIDAY use the same service)."""
    global _service
    with _service_lock:
        if _service is None:
            def on_event(name, detail):
                label = _EVENT_LABELS.get(name)
                if label and player is not None:
                    try:
                        player.write_log(f"WEB: {label} — {detail[:80]}")
                    except Exception:
                        pass
            _service = WebIntelligenceService(on_event=on_event)
        return _service


def shutdown_service() -> dict:
    """Called from the app's shutdown path. Safe when never started."""
    global _service
    with _service_lock:
        if _service is None:
            return {"child_processes_stopped": 0, "live_children": 0}
        report = _service.shutdown()
        _service = None
        return report


def cancel_current() -> str:
    with _service_lock:
        svc = _service
    if svc is None:
        return "No web task is running."
    stopped = svc.cancel()
    return f"Stopped the web task ({stopped} child process(es) ended)."


# ---- formatting (persona = tone, not facts) -------------------------------

def _fmt_results(results, persona: str) -> str:
    lines = []
    for i, r in enumerate(results[:5], 1):
        snippet = (r.snippet or "").strip()
        lines.append(f"{i}. {r.title} — {r.url}"
                     + (f"\n   {snippet[:160]}" if snippet else ""))
    body = "\n".join(lines)
    if persona == "friday":
        return f"Here's what I found:\n{body}"
    return f"Search results, sir:\n{body}"


def _fmt_document(doc, persona: str) -> str:
    head = f"{doc.title} ({doc.canonical_url})"
    warn = ""
    if doc.injection_flags:
        warn = ("\n[Note: this page contained text addressed to automated "
                "agents; it was treated as untrusted data, not instructions.]")
    return f"{head}{warn}\n\n{doc.cleaned_text[:8000]}"


def _fmt_research(answer, persona: str) -> str:
    cites = "\n".join(f"  [{c.citation_id}] {c.source_title} — {c.canonical_url}"
                      for c in answer.citations)
    limits = "; ".join(answer.limitations)
    return (f"{answer.answer[:20_000]}\n\n"
            f"Sources ({answer.sources_checked} read, confidence "
            f"{answer.confidence}):\n{cites}\n"
            f"Limitations: {limits}")


# ---- the tool entry point --------------------------------------------------

def web_intelligence(parameters: dict, player=None) -> str:
    """Dispatch by mode, with one event on either side.

    `mode` is a fixed enum, so it is safe to record. `query` and `url` are user
    content and never leave this function.
    """
    import time as _time

    from aoca.events import EventState, emit

    mode = str(parameters.get("mode", "quick_search")).strip().lower()
    started_at = _time.monotonic()
    emit("web.started", EventState.EXECUTING, tool="web_intelligence",
         action=mode)
    try:
        result = _web_intelligence(parameters, player)
    except Exception as exc:
        emit("web.finished", EventState.FAILED, tool="web_intelligence",
             action=mode, error_code=type(exc).__name__,
             duration_ms=int((_time.monotonic() - started_at) * 1000))
        raise
    emit("web.finished", EventState.EXECUTED, tool="web_intelligence",
         action=mode, execution_started=True,
         size_bytes=len(result or ""),
         duration_ms=int((_time.monotonic() - started_at) * 1000))
    return result


def _web_intelligence(parameters: dict, player=None) -> str:
    """Dispatch by mode. Returns real retrieved content or an honest error —
    never a fabricated result."""
    mode = str(parameters.get("mode", "quick_search")).strip().lower()
    query = str(parameters.get("query", "")).strip()
    url = str(parameters.get("url", "")).strip()
    persona = str(parameters.get("persona", "friday")).strip().lower()
    svc = get_service(player)

    try:
        if mode == "status":
            statuses = svc.provider_status()
            up = [s.name for s in statuses if s.available]
            down = [f"{s.name} ({s.detail})" for s in statuses if not s.available]
            out = "Web providers — available: " + (", ".join(up) or "none")
            if down:
                out += ". Unavailable: " + "; ".join(down)
            return out

        if mode == "whats_happening":
            snap = svc.state.snapshot()
            if snap["phase"] == "idle":
                return "No web task is running."
            return (f"Current task: {snap['task']} — phase {snap['phase']}, "
                    f"{snap['sources_found']} sources so far, "
                    f"running {snap['running_seconds']}s.")

        if mode == "cancel":
            return cancel_current()

        if mode == "read_url":
            if not url:
                return "I need a URL to read."
            doc = svc.read_url(url)
            return _fmt_document(doc, persona)

        if mode == "read_rss":
            if not url:
                return "I need a feed URL."
            entries = svc.read_rss(url)
            return _fmt_results(entries, persona)

        if mode == "github_search":
            if not query:
                return "I need a search query."
            return _fmt_results(svc.search_github(query), persona)

        if mode == "deep_research":
            if not query:
                return "I need a research question."
            answer = svc.deep_research(WebQuery(
                query=query, requested_action="deep_research",
                research_depth=int(parameters.get("depth", 1) or 1),
                assistant_id=persona))
            return _fmt_research(answer, persona)

        # default: quick_search
        if not query:
            return "I need a search query."
        results = svc.quick_search(WebQuery(
            query=query, max_results=int(parameters.get("max_results", 5) or 5),
            assistant_id=persona))
        return _fmt_results(results, persona)

    except ProviderError as exc:
        return f"Web lookup failed: {redact_secrets(str(exc))}"
    except InterruptedError:
        return "The web task was cancelled."
    except Exception as exc:
        log.exception("web_intelligence tool error")
        return f"Web intelligence hit an unexpected error: {redact_secrets(str(exc))[:200]}"
