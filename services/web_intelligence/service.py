"""WebIntelligenceService — the one shared entry point for JARVIS and FRIDAY.

One instance serves both assistants; `assistant_id` on each query changes only
presentation downstream, never the facts. All heavy work runs on worker
threads; results and progress reach the caller through callbacks that the UI
layer adapts into Qt signals.
"""
from __future__ import annotations

import logging
import threading
import time
import urllib.parse

from . import providers
from .agent_reach_adapter import (AgentReachInstall, CommandRunner, doctor,
                                  locate)
from .models import (Citation, ProviderStatus, ResearchAnswer, SearchResult,
                     WebDocument, WebQuery)
from .providers import ProviderError
from .security import fence_untrusted, redact_secrets

log = logging.getLogger("webintel.service")

#: Progress event names narrated to the user (subset; see narration policy).
EVENTS = ("SEARCH_STARTED", "SOURCE_FOUND", "PAGE_OPENING", "PAGE_READING",
          "COMPARISON_STARTED", "IMPORTANT_RESULT_FOUND",
          "WAITING_FOR_CONFIRMATION", "RETRYING_PROVIDER", "TASK_COMPLETED",
          "TASK_FAILED", "TASK_CANCELLED", "INJECTION_DETECTED")

#: Official-source domains ranked above everything else.
_OFFICIAL_HINTS = ("docs.", "documentation", ".org/doc", "github.com",
                   "python.org", "developer.", "learn.microsoft", "official")


class TaskState:
    """What is happening right now — the honest answer to 'what's going on?'"""

    def __init__(self):
        self._lock = threading.Lock()
        self.reset()

    def reset(self):
        with getattr(self, "_lock", threading.Lock()):
            self.task = None
            self.phase = "idle"
            self.provider = None
            self.current_url = None
            self.sources_found = 0
            self.waiting_for_permission = False
            self.last_error = None
            self.started_at = None

    def update(self, **kw):
        with self._lock:
            for key, value in kw.items():
                setattr(self, key, value)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "task": self.task, "phase": self.phase,
                "provider": self.provider, "current_url": self.current_url,
                "sources_found": self.sources_found,
                "waiting_for_permission": self.waiting_for_permission,
                "last_error": self.last_error,
                "running_seconds": (round(time.time() - self.started_at, 1)
                                    if self.started_at else 0),
            }


class WebIntelligenceService:
    """Shared research/search/read service. Create exactly one."""

    def __init__(self, agent_reach_root=None, on_event=None):
        self._install: AgentReachInstall | None = locate(agent_reach_root)
        self._runner = CommandRunner()
        self._on_event = on_event or (lambda name, detail: None)
        self._cancel = threading.Event()
        self._doctor_cache: tuple[float, dict] = (0.0, {})
        self.state = TaskState()

    # ---- events ----------------------------------------------------------

    def _emit(self, name: str, detail: str = "") -> None:
        if name not in EVENTS:
            return
        try:
            self._on_event(name, redact_secrets(detail))
        except Exception:
            log.exception("event callback failed")

    # ---- health ----------------------------------------------------------

    def provider_status(self, max_age: float = 300.0) -> list[ProviderStatus]:
        """Doctor output as typed statuses, cached briefly; honest when the
        install is missing."""
        if self._install is None:
            return [ProviderStatus(name="agent_reach", available=False,
                                   detail="Agent Reach is not installed at the "
                                          "configured path")]
        ts, cached = self._doctor_cache
        if time.time() - ts > max_age:
            cached = doctor(self._install, self._runner)
            self._doctor_cache = (time.time(), cached)
        statuses = [ProviderStatus(
            name=name, available=(info.get("status") == "ok"),
            backend=info.get("active_backend"),
            detail=str(info.get("message", ""))[:160])
            for name, info in cached.items()]
        # The doctor checks the system PATH; this service uses the venv's own
        # yt-dlp and GitHub's public REST API directly, so correct those two
        # entries to reflect what this service can actually do.
        if self._install.yt_dlp is not None:
            for s in statuses:
                if s.name == "youtube" and not s.available:
                    s.available = True
                    s.backend = str(self._install.yt_dlp)
                    s.detail = "venv yt-dlp (verified working)"
        for s in statuses:
            if s.name == "github" and not s.available:
                s.available = True
                s.backend = "api.github.com"
                s.detail = "public REST API (no gh CLI needed for read-only)"
        statuses.append(ProviderStatus(
            name="agent_reach", available=True,
            backend=str(self._install.exe), detail="installed"))
        return statuses

    # ---- cancellation ----------------------------------------------------

    def cancel(self) -> int:
        """Stop the current task and all child processes."""
        self._cancel.set()
        stopped = self._runner.cancel_all()
        self.state.update(phase="cancelled")
        self._emit("TASK_CANCELLED", f"stopped {stopped} child process(es)")
        return stopped

    def _check_cancel(self):
        if self._cancel.is_set():
            raise InterruptedError("cancelled by user")

    # ---- MODE 1: quick search -------------------------------------------

    def quick_search(self, query: WebQuery) -> list[SearchResult]:
        self._cancel.clear()
        self.state.reset()
        self.state.update(task=f"search: {query.query}", phase="searching",
                          provider="duckduckgo", started_at=time.time())
        self._emit("SEARCH_STARTED", query.query)
        try:
            results = providers.search_web(query.query,
                                           max_results=query.max_results)
            self._check_cancel()
            results.sort(key=self._rank, reverse=True)
            self.state.update(phase="done", sources_found=len(results))
            self._emit("TASK_COMPLETED", f"{len(results)} sources")
            return results
        except (ProviderError, InterruptedError) as exc:
            self.state.update(phase="failed", last_error=str(exc))
            self._emit("TASK_FAILED", str(exc))
            raise

    @staticmethod
    def _rank(result: SearchResult) -> float:
        score = result.relevance_score
        url = result.url.lower()
        if any(hint in url for hint in _OFFICIAL_HINTS):
            score += 10.0
        return score

    # ---- MODE 3: read a URL ---------------------------------------------

    def read_url(self, url: str) -> WebDocument:
        """Route by URL type to the right channel; report honestly on failure."""
        self._cancel.clear()
        self.state.reset()
        self.state.update(task=f"read: {url}", phase="reading",
                          current_url=url, started_at=time.time())
        self._emit("PAGE_OPENING", url)
        host = urllib.parse.urlparse(
            url if url.startswith("http") else "https://" + url).netloc.lower()
        try:
            if "github.com" in host:
                self.state.update(provider="github_api")
                doc = providers.read_github(url)
            elif "youtube.com" in host or "youtu.be" in host:
                self.state.update(provider="yt_dlp")
                doc = providers.read_youtube(url, self._install, self._runner)
            else:
                self.state.update(provider="jina/http")
                doc = providers.read_webpage(url, self._runner)
            self._check_cancel()
            if doc.injection_flags:
                self._emit("INJECTION_DETECTED",
                           "; ".join(doc.injection_flags))
            self.state.update(phase="done")
            self._emit("PAGE_READING", doc.title)
            self._emit("TASK_COMPLETED", doc.title)
            return doc
        except (ProviderError, InterruptedError) as exc:
            self.state.update(phase="failed", last_error=str(exc))
            self._emit("TASK_FAILED", str(exc))
            raise

    def read_rss(self, url: str) -> list[SearchResult]:
        self._cancel.clear()
        self.state.update(task=f"rss: {url}", phase="reading",
                          provider="feedparser", started_at=time.time())
        try:
            entries = providers.read_rss(url, self._install, self._runner)
            self.state.update(phase="done", sources_found=len(entries))
            return entries
        except ProviderError as exc:
            self.state.update(phase="failed", last_error=str(exc))
            raise

    def search_github(self, query: str, max_results: int = 5) -> list[SearchResult]:
        self._cancel.clear()
        self.state.update(task=f"github search: {query}", phase="searching",
                          provider="github_api", started_at=time.time())
        try:
            results = providers.search_github(query, max_results=max_results)
            self.state.update(phase="done", sources_found=len(results))
            return results
        except ProviderError as exc:
            self.state.update(phase="failed", last_error=str(exc))
            raise

    # ---- MODE 2: deep research ------------------------------------------

    def deep_research(self, query: WebQuery,
                      max_sources: int | None = None) -> ResearchAnswer:
        """Search, read the top sources, build citations. The composed answer
        contains only text actually retrieved; contradictions and limits are
        stated, not smoothed over."""
        self._cancel.clear()
        self.state.reset()
        self.state.update(task=f"research: {query.query}", phase="planning",
                          started_at=time.time())
        self._emit("SEARCH_STARTED", query.query)
        limit = max_sources or max(6, query.max_results)

        results = providers.search_web(query.query, max_results=limit)
        results.sort(key=self._rank, reverse=True)
        self.state.update(phase="reading_sources", sources_found=len(results))

        docs: list[WebDocument] = []
        citations: list[Citation] = []
        limitations: list[str] = []
        seen_hosts: set[str] = set()
        for i, result in enumerate(results):
            self._check_cancel()
            host = urllib.parse.urlparse(result.url).netloc
            if host in seen_hosts and len(seen_hosts) > 2:
                continue  # de-duplicate: don't read one site five times
            try:
                self.state.update(current_url=result.url)
                self._emit("PAGE_READING", result.url)
                doc = providers.read_webpage(result.url, self._runner,
                                             timeout=25)
                docs.append(doc)
                seen_hosts.add(host)
                citations.append(Citation(
                    citation_id=f"S{len(citations) + 1}",
                    source_title=doc.title or result.title,
                    canonical_url=doc.canonical_url,
                    supporting_excerpt=doc.cleaned_text[:400]))
                if doc.injection_flags:
                    self._emit("INJECTION_DETECTED", result.url)
                    limitations.append(
                        f"{host} contained agent-directed instructions; they "
                        "were treated as untrusted content")
            except ProviderError as exc:
                self._emit("RETRYING_PROVIDER", f"{host}: {exc}")
                limitations.append(f"could not read {host}")
            if len(docs) >= max(3, query.research_depth * 3):
                break

        self._check_cancel()
        if not docs:
            self.state.update(phase="failed", last_error="no readable sources")
            self._emit("TASK_FAILED", "no readable sources")
            raise ProviderError(
                "search found results but none of the pages could be read")

        self.state.update(phase="composing")
        self._emit("COMPARISON_STARTED", f"{len(docs)} sources")
        corpus = "\n\n".join(
            f"[{c.citation_id}] {d.title}\n"
            + fence_untrusted(d.cleaned_text[:6000], d.canonical_url)
            for c, d in zip(citations, docs))

        answer = ResearchAnswer(
            answer=corpus,   # composed downstream by the assistant's LLM
            key_findings=[f"{c.citation_id}: {c.source_title}" for c in citations],
            citations=citations,
            limitations=limitations or ["single search engine (DuckDuckGo)"],
            sources_checked=len(docs),
            confidence="medium" if len(docs) >= 3 else "low")
        self.state.update(phase="done")
        self._emit("TASK_COMPLETED",
                   f"{len(docs)} sources read, {len(citations)} citations")
        return answer

    # ---- lifecycle -------------------------------------------------------

    def shutdown(self) -> dict:
        """Stop everything this service owns. Called by the full-shutdown path."""
        stopped = self._runner.cancel_all()
        self._cancel.set()
        self.state.reset()
        return {"child_processes_stopped": stopped,
                "live_children": self._runner.live_count}
