"""Ruflo vector-memory bridge — semantic recall for Jarvis/Friday.

Design: memory/long_term.json stays the source of truth (it is injected
into the system prompt at connect time). Every saved fact is ALSO mirrored
into ruflo's vector DB (.claude/memory.db, D drive) so Jarvis can answer
"what do you know about X" by meaning, not exact key match.

All calls shell out to the project-local ruflo CLI via node. Writes are
fire-and-forget on a background thread; search is synchronous (~1s).
Graceful degradation: if node/ruflo is missing, everything no-ops.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import threading
from pathlib import Path

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent

RUFLO_JS  = BASE_DIR / "node_modules" / "ruflo" / "bin" / "ruflo.js"
NODE      = shutil.which("node")
NAMESPACE = "jarvis"
_TIMEOUT  = 30


def available() -> bool:
    return bool(NODE) and RUFLO_JS.exists()


def _run(*args: str) -> str:
    """Run a ruflo CLI command from the project root; return stdout."""
    if not available():
        return ""
    try:
        r = subprocess.run(
            [NODE, str(RUFLO_JS), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=_TIMEOUT, cwd=str(BASE_DIR),
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return r.stdout or ""
    except Exception as e:
        print(f"[RufloMemory] CLI error: {e}")
        return ""


def store(category: str, key: str, value: str) -> None:
    """Mirror one fact into the vector DB. Blocking (~0.5s) — see store_async."""
    if not str(value).strip():
        return
    _run("memory", "store",
         "-k", f"{category}/{key}",
         "-v", f"{category} - {key}: {value}",
         "--namespace", NAMESPACE)


def store_async(category: str, key: str, value: str) -> None:
    """Fire-and-forget mirror write; never blocks the voice loop."""
    threading.Thread(target=store, args=(category, key, value),
                     daemon=True, name="RufloStore").start()


def delete(category: str, key: str) -> None:
    _run("memory", "delete", "-k", f"{category}/{key}", "--namespace", NAMESPACE)


def search(query: str, limit: int = 5) -> str:
    """Semantic search; returns matching facts as plain lines, '' if none."""
    out = _run("memory", "search", "-q", query,
               "--namespace", NAMESPACE, "--limit", str(limit))
    lines = []
    for row in out.splitlines():
        # table rows look like: | category/key | 0.42 | jarvis | preview... |
        if row.startswith("|") and "/" in row and "Namespace" not in row:
            cells = [c.strip() for c in row.strip("|").split("|")]
            if len(cells) >= 4 and cells[0]:
                lines.append(f"{cells[0]}: {cells[3]}")
    return "\n".join(lines)


def sync_all() -> int:
    """Push every fact in long_term.json into ruflo. Returns count."""
    path = BASE_DIR / "memory" / "long_term.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"[RufloMemory] Cannot read long_term.json: {e}")
        return 0
    n = 0
    for category, entries in data.items():
        if not isinstance(entries, dict):
            continue
        for key, entry in entries.items():
            value = entry.get("value") if isinstance(entry, dict) else entry
            if value:
                store(category, key, str(value))
                n += 1
    print(f"[RufloMemory] Synced {n} facts into ruflo ({NAMESPACE})")
    return n


def demo():
    print("available:", available())
    if not available():
        print("ruflo bridge demo SKIPPED (no node/ruflo)")
        return
    store("notes", "_bridge_test", "the bridge self-check fact about violet parrots")
    hits = search("violet parrot fact", limit=3)
    print("search hits:\n" + hits)
    assert "_bridge_test" in hits
    delete("notes", "_bridge_test")
    print("ruflo bridge demo OK")


if __name__ == "__main__":
    if "--sync" in sys.argv:
        sync_all()
    else:
        demo()
