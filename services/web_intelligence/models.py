"""Typed result models for Web Intelligence.

Dataclasses (matching the codebase's existing style — no Pydantic dependency
anywhere in the project). Every model carries enough provenance that an answer
can always say where a fact came from; nothing here stores credentials.
"""
from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field, asdict


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


@dataclass
class WebQuery:
    """One user request, normalized before routing."""
    query: str
    requested_action: str = "quick_search"   # quick_search|deep_research|read_url|browse|explain_page|platform
    source_preferences: list = field(default_factory=list)
    time_range: str | None = None
    max_results: int = 5
    research_depth: int = 1
    visible_browser_requested: bool = False
    assistant_id: str = "friday"             # jarvis|friday — presentation only
    user_confirmation_state: str = "none"    # none|pending|granted


@dataclass
class SearchResult:
    title: str
    url: str
    source_name: str = ""
    source_type: str = "web"                 # web|github|youtube|rss|docs
    snippet: str = ""
    published_at: str | None = None
    author: str | None = None
    confidence: float = 0.5
    relevance_score: float = 0.0
    retrieved_at: str = field(default_factory=_now_iso)


@dataclass
class WebDocument:
    canonical_url: str
    title: str
    cleaned_text: str
    headings: list = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    extraction_method: str = "http"          # http|jina|playwright|gh_api|yt_dlp|feedparser
    source_type: str = "web"
    retrieved_at: str = field(default_factory=_now_iso)
    injection_flags: list = field(default_factory=list)

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.cleaned_text.encode("utf-8", "replace")).hexdigest()[:16]


@dataclass
class Citation:
    citation_id: str
    source_title: str
    canonical_url: str
    supporting_excerpt: str = ""
    claim_ids: list = field(default_factory=list)
    retrieval_time: str = field(default_factory=_now_iso)


@dataclass
class ResearchAnswer:
    answer: str
    key_findings: list = field(default_factory=list)
    citations: list = field(default_factory=list)      # list[Citation]
    contradictions: list = field(default_factory=list)
    limitations: list = field(default_factory=list)
    sources_checked: int = 0
    confidence: str = "medium"               # low|medium|high — never fabricated precision

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProviderStatus:
    """One channel's honest availability, from doctor + local probes."""
    name: str
    available: bool
    backend: str | None = None
    detail: str = ""
    checked_at: str = field(default_factory=_now_iso)
