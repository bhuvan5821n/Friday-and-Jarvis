"""Content providers: read a URL, search the web, GitHub, YouTube, RSS.

Design rule: every function returns real retrieved content or raises
ProviderError with an honest reason. There is no silent fallback to
fabricated text anywhere in this module.

Fallback chains (per the doctor's actual state on this machine):
  webpage : Jina Reader -> direct HTTP+BeautifulSoup -> (Phase 4: Playwright)
  search  : DDGS (already a project dependency) -> Jina search
  github  : GitHub public REST API (no gh CLI installed) -> webpage reader
  youtube : Agent Reach venv yt-dlp (subtitles/metadata) -> honest failure
  rss     : feedparser via Agent Reach venv python -> raw HTTP + parse
"""
from __future__ import annotations

import json
import logging
import re
import urllib.parse
import urllib.request

from ..agent_reach_adapter import AgentReachInstall, CommandRunner
from ..models import SearchResult, WebDocument
from ..security import scan_for_injection

log = logging.getLogger("webintel.providers")

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"


class ProviderError(RuntimeError):
    """A provider failed for a stated reason. Callers may try the next one."""


def _http_get(url: str, timeout: float = 30.0, accept: str = "text/html") -> str:
    req = urllib.request.Request(url, headers={"User-Agent": _UA, "Accept": accept})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace")


def _validate_url(url: str) -> str:
    url = url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url
    parsed = urllib.parse.urlparse(url)
    if not parsed.netloc or " " in parsed.netloc:
        raise ProviderError(f"not a valid URL: {url!r}")
    return url


# ---- webpage -------------------------------------------------------------

def read_webpage(url: str, runner: CommandRunner | None = None,
                 timeout: float = 30.0) -> WebDocument:
    """Read any public URL as cleaned text. Jina Reader first (same channel
    Agent Reach routes to), then direct HTTP extraction."""
    url = _validate_url(url)
    errors = []

    try:
        text = _http_get(f"https://r.jina.ai/{url}", timeout=timeout,
                         accept="text/plain")
        if text.strip():
            title = ""
            m = re.match(r"Title:\s*(.+)", text)
            if m:
                title = m.group(1).strip()
            doc = WebDocument(canonical_url=url, title=title or url,
                              cleaned_text=text[:120_000],
                              extraction_method="jina")
            doc.injection_flags = scan_for_injection(doc.cleaned_text)
            return doc
    except Exception as exc:
        errors.append(f"jina: {exc}")

    try:
        html = _http_get(url, timeout=timeout)
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        title = (soup.title.string or "").strip() if soup.title else url
        headings = [h.get_text(" ", strip=True)
                    for h in soup.find_all(["h1", "h2", "h3"])[:40]]
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n", strip=True))
        doc = WebDocument(canonical_url=url, title=title,
                          cleaned_text=text[:120_000], headings=headings,
                          extraction_method="http")
        doc.injection_flags = scan_for_injection(doc.cleaned_text)
        return doc
    except Exception as exc:
        errors.append(f"http: {exc}")

    raise ProviderError("could not read the page — " + "; ".join(errors))


# ---- web search ----------------------------------------------------------

def search_web(query: str, max_results: int = 5,
               timeout: float = 30.0) -> list[SearchResult]:
    """Public web search.

    Primary: DDG Lite HTML endpoint (verified to return correct anchors).
    Fallback: the assistant's existing DDGS wrapper (actions/web_search.py) —
    its installed package is dated and sometimes returns unrelated results,
    so it is only consulted when the primary parser comes back empty.
    Raises rather than returning fabricated results.
    """
    raw = _ddg_html_search(query, max_results, timeout)

    if not raw:
        try:
            from actions.web_search import _ddg_search
            raw = _ddg_search(query, max_results=max_results)
        except Exception as exc:
            log.info("ddg wrapper failed: %s", exc)

    results = [
        SearchResult(title=r.get("title", ""), url=r.get("url", ""),
                     snippet=r.get("snippet", ""), source_name="duckduckgo",
                     source_type="web")
        for r in raw if r.get("url")
    ]
    if not results:
        raise ProviderError(
            "web search returned no results (provider may be rate-limiting)")
    return results


def _ddg_html_search(query: str, max_results: int,
                     timeout: float) -> list[dict]:
    """Parse DuckDuckGo's Lite endpoint — plain HTML, no JS, no API key.

    (html.duckduckgo.com now serves an empty JS shell to plain HTTP clients;
    lite.duckduckgo.com still returns real anchors with class 'result-link'.
    NB: lite serves a block page to partial browser UA strings but answers a
    plain 'Mozilla/5.0' — verified empirically, so this request does not use
    the module-wide _UA.)
    """
    try:
        req = urllib.request.Request(
            "https://lite.duckduckgo.com/lite/?q=" + urllib.parse.quote(query),
            headers={"User-Agent": "Mozilla/5.0", "Accept": "text/html"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            html = resp.read().decode("utf-8", "replace")
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        out = []
        # Layout: <a class='result-link'> in one table row; the snippet lives
        # in the following row's 'result-snippet' cell.
        snippets = [td.get_text(" ", strip=True)
                    for td in soup.select(".result-snippet")]
        for i, link in enumerate(soup.select("a.result-link")[:max_results]):
            href = link.get("href", "")
            if "uddg=" in href:  # DDG redirect wrapper
                href = urllib.parse.unquote(
                    href.split("uddg=")[1].split("&")[0])
            if not href.startswith("http"):
                continue
            out.append({"title": link.get_text(" ", strip=True),
                        "url": href,
                        "snippet": snippets[i] if i < len(snippets) else ""})
        return out
    except Exception as exc:
        log.info("ddg lite fallback failed: %s", exc)
        return []


# ---- GitHub --------------------------------------------------------------

_GH_REPO = re.compile(r"github\.com/([\w.-]+)/([\w.-]+)")


def read_github(url_or_repo: str, timeout: float = 30.0) -> WebDocument:
    """Public repository overview via GitHub's REST API (no gh CLI installed
    on this machine — the doctor reports the channel as 'warn')."""
    m = _GH_REPO.search(url_or_repo)
    if m:
        owner, repo = m.group(1), m.group(2).removesuffix(".git")
    elif "/" in url_or_repo and " " not in url_or_repo:
        owner, repo = url_or_repo.split("/", 1)
    else:
        raise ProviderError(f"cannot parse GitHub repo from {url_or_repo!r}")

    api = f"https://api.github.com/repos/{owner}/{repo}"
    try:
        info = json.loads(_http_get(api, timeout=timeout,
                                    accept="application/vnd.github+json"))
    except Exception as exc:
        raise ProviderError(f"GitHub API unreachable for {owner}/{repo}: {exc}")

    readme_text = ""
    try:
        readme = json.loads(_http_get(
            api + "/readme", timeout=timeout,
            accept="application/vnd.github+json"))
        import base64
        readme_text = base64.b64decode(readme.get("content", "")).decode(
            "utf-8", "replace")[:60_000]
    except Exception:
        readme_text = "(README unavailable)"

    body = (
        f"{info.get('full_name')}\n{info.get('description') or ''}\n"
        f"Stars: {info.get('stargazers_count')}  Forks: {info.get('forks_count')}  "
        f"Language: {info.get('language')}  License: {(info.get('license') or {}).get('spdx_id')}\n"
        f"Updated: {info.get('updated_at')}\nTopics: {', '.join(info.get('topics', []))}\n"
        f"\n--- README ---\n{readme_text}")
    doc = WebDocument(
        canonical_url=info.get("html_url", f"https://github.com/{owner}/{repo}"),
        title=info.get("full_name", f"{owner}/{repo}"),
        cleaned_text=body, extraction_method="gh_api", source_type="github",
        metadata={"stars": info.get("stargazers_count"),
                  "language": info.get("language"),
                  "default_branch": info.get("default_branch")})
    doc.injection_flags = scan_for_injection(readme_text)
    return doc


def search_github(query: str, max_results: int = 5,
                  timeout: float = 30.0) -> list[SearchResult]:
    api = ("https://api.github.com/search/repositories?q="
           + urllib.parse.quote(query) + f"&per_page={max_results}")
    try:
        data = json.loads(_http_get(api, timeout=timeout,
                                    accept="application/vnd.github+json"))
    except Exception as exc:
        raise ProviderError(f"GitHub search unavailable: {exc}")
    return [
        SearchResult(title=item.get("full_name", ""),
                     url=item.get("html_url", ""),
                     snippet=(item.get("description") or "")[:300],
                     source_name="github", source_type="github",
                     published_at=item.get("updated_at"),
                     relevance_score=float(item.get("stargazers_count", 0)))
        for item in data.get("items", [])
    ]


# ---- YouTube -------------------------------------------------------------

def read_youtube(url: str, install: AgentReachInstall | None,
                 runner: CommandRunner, timeout: float = 90.0) -> WebDocument:
    """Metadata + subtitles via Agent Reach's own yt-dlp. States plainly when
    captions don't exist — never invents video content."""
    url = _validate_url(url)
    if install is None or install.yt_dlp is None:
        raise ProviderError("yt-dlp is not available (Agent Reach venv not found)")

    rc, out, err = runner.run(
        # -4: this machine's IPv6 route to YouTube stalls (2m44s vs 3.6s,
        # measured); core/runtime.py works around the same issue app-wide.
        [install.yt_dlp, "-4", "--socket-timeout", "10", "--skip-download",
         "--dump-json", "--no-playlist", "--no-warnings", url],
        timeout=timeout)
    if rc != 0 or not out.strip():
        raise ProviderError(f"yt-dlp could not read the video: {err.strip()[:200]}")
    meta = json.loads(out.splitlines()[0])

    transcript = ""
    subs = meta.get("subtitles") or {}
    autos = meta.get("automatic_captions") or {}
    lang = next((l for l in ("en", "en-US", "en-orig") if l in subs or l in autos), None)
    if lang:
        entries = (subs.get(lang) or autos.get(lang) or [])
        sub_url = next((e.get("url") for e in entries
                        if e.get("ext") in ("vtt", "srv3", "json3")), None)
        if sub_url:
            try:
                raw = _http_get(sub_url, timeout=30, accept="*/*")
                if raw.lstrip().startswith("{"):
                    # json3: {"events": [{"segs": [{"utf8": "..."}]}]}
                    data = json.loads(raw)
                    transcript = " ".join(
                        seg.get("utf8", "")
                        for ev in data.get("events", [])
                        for seg in (ev.get("segs") or []))
                    transcript = re.sub(r"\s+", " ", transcript).strip()[:80_000]
                else:
                    transcript = re.sub(
                        r"WEBVTT.*?\n\n|\d\d:\d\d:\d\d[.,]\d+ --> .*?\n|<[^>]+>",
                        "", raw)[:80_000]
                    transcript = re.sub(r"\n{2,}", "\n", transcript).strip()
            except Exception as exc:
                log.info("subtitle fetch failed: %s", exc)

    body = (f"{meta.get('title')}\nChannel: {meta.get('channel')}  "
            f"Duration: {meta.get('duration_string')}  Views: {meta.get('view_count')}\n"
            f"Uploaded: {meta.get('upload_date')}\n\n"
            f"Description:\n{(meta.get('description') or '')[:4000]}\n")
    if transcript:
        body += f"\n--- Transcript ---\n{transcript}"
    else:
        body += "\n[No captions are available for this video — transcript cannot be provided.]"

    doc = WebDocument(
        canonical_url=meta.get("webpage_url", url), title=meta.get("title", url),
        cleaned_text=body, extraction_method="yt_dlp", source_type="youtube",
        metadata={"channel": meta.get("channel"),
                  "duration": meta.get("duration"),
                  "has_transcript": bool(transcript)})
    doc.injection_flags = scan_for_injection(doc.cleaned_text)
    return doc


# ---- RSS -----------------------------------------------------------------

def read_rss(url: str, install: AgentReachInstall | None,
             runner: CommandRunner, timeout: float = 30.0) -> list[SearchResult]:
    """Feed entries via feedparser inside the Agent Reach venv (this repo's
    venv does not ship feedparser; reusing theirs avoids a new dependency)."""
    url = _validate_url(url)
    if install is None:
        raise ProviderError("feedparser unavailable (Agent Reach venv not found)")
    # Fetch here (UA + hard timeout — feedparser's own fetcher can hang on
    # servers that dislike its default agent), parse in the venv from stdin.
    try:
        raw_feed = _http_get(url, timeout=timeout, accept="application/rss+xml, application/atom+xml, */*")
    except Exception as exc:
        raise ProviderError(f"feed unreachable: {exc}")
    script = (
        "import sys, json, feedparser\n"
        "d = feedparser.parse(sys.stdin.read())\n"
        "print(json.dumps([{'title': e.get('title',''), 'link': e.get('link',''),"
        " 'summary': e.get('summary','')[:400], 'published': e.get('published','')}"
        " for e in d.entries[:20]]))\n")
    import subprocess as _sp
    proc = _sp.Popen([str(install.python), "-c", script],
                     stdin=_sp.PIPE, stdout=_sp.PIPE, stderr=_sp.PIPE,
                     text=True, encoding="utf-8", errors="replace",
                     creationflags=getattr(_sp, "CREATE_NO_WINDOW", 0))
    try:
        out, err = proc.communicate(input=raw_feed, timeout=timeout)
    except _sp.TimeoutExpired:
        proc.kill()
        raise ProviderError("feed parsing timed out")
    if proc.returncode != 0 or not out.strip():
        raise ProviderError(f"RSS parse failed: {(err or '').strip()[:200]}")
    entries = json.loads(out)
    if not entries:
        raise ProviderError("the feed has no entries (or is not RSS/Atom)")
    return [
        SearchResult(title=e["title"], url=e["link"], snippet=e["summary"],
                     source_name="rss", source_type="rss",
                     published_at=e.get("published") or None)
        for e in entries
    ]
