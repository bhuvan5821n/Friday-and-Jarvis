import json
import os
from datetime import datetime
from threading import Lock
from pathlib import Path
import sys


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR         = get_base_dir()
MEMORY_PATH      = BASE_DIR / "memory" / "long_term.json"
_lock            = Lock()
MAX_VALUE_LENGTH = 380
MEMORY_MAX_CHARS = 6000   # raised from 2200 — Gemini context affords a fuller life memory

# Default importance per category (1-5). Who the user IS outranks passing notes.
CATEGORY_IMPORTANCE = {
    "identity": 5, "relationships": 4, "projects": 4,
    "preferences": 3, "wishes": 2, "notes": 2,
}

def _empty_memory() -> dict:
    return {
        "identity":      {},
        "preferences":   {},
        "projects":      {},
        "relationships": {},
        "wishes":        {},
        "notes":         {},
    }

def load_memory() -> dict:
    if not MEMORY_PATH.exists():
        return _empty_memory()
    with _lock:
        try:
            data = json.loads(MEMORY_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                base = _empty_memory()
                for key in base:
                    if key not in data:
                        data[key] = {}
                return data
            return _empty_memory()
        except Exception as e:
            print(f"[Memory] Load error: {e}")
            return _empty_memory()

def _all_entries(memory: dict) -> list[tuple]:
    entries = []
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        for key, entry in items.items():
            if isinstance(entry, dict) and "value" in entry:
                entries.append((cat, key, entry))
    return entries


def _score(cat: str, entry: dict) -> tuple:
    """Sort key: importance, then how often it resurfaces, then recency."""
    imp  = entry.get("importance", CATEGORY_IMPORTANCE.get(cat, 2))
    freq = entry.get("freq", 1)
    return (imp, min(freq, 9), entry.get("updated", "0000-00-00"))


def _dedupe(memory: dict) -> None:
    """Merge duplicate values within a category: keep the higher-scored key."""
    for cat, items in memory.items():
        if not isinstance(items, dict):
            continue
        by_val: dict[str, str] = {}   # normalized value -> key kept
        for key in list(items.keys()):
            entry = items[key]
            if not (isinstance(entry, dict) and "value" in entry):
                continue
            norm = str(entry["value"]).strip().lower()
            other = by_val.get(norm)
            if other is None:
                by_val[norm] = key
            else:
                keep, drop = (key, other) if _score(cat, items[key]) >= _score(cat, items[other]) else (other, key)
                items[keep]["freq"] = items[keep].get("freq", 1) + items[drop].get("freq", 1)
                del items[drop]
                by_val[norm] = keep
                print(f"[Memory] merged duplicate {cat}/{drop} -> {keep}")


def _trim_to_limit(memory: dict) -> dict:
    if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
        return memory
    # Drop lowest-scored first; identity only as a last resort.
    entries = _all_entries(memory)
    entries.sort(key=lambda t: ((t[0] == "identity"), _score(t[0], t[2])))
    for cat, key, _ in entries:
        if len(json.dumps(memory, ensure_ascii=False)) <= MEMORY_MAX_CHARS:
            break
        del memory[cat][key]
        print(f"[Memory] Trimmed {cat}/{key}")
    return memory

def save_memory(memory: dict) -> None:
    """Save memory with atomic write to prevent corruption on crash."""
    if not isinstance(memory, dict):
        return
    _dedupe(memory)
    memory = _trim_to_limit(memory)
    MEMORY_PATH.parent.mkdir(parents=True, exist_ok=True)
    
    # Atomic write: write to temp file first, then rename
    import tempfile
    with _lock:
        try:
            tmp_fd, tmp_path = tempfile.mkstemp(
                dir=str(MEMORY_PATH.parent),
                suffix='.tmp',
                prefix='memory_'
            )
            try:
                with os.fdopen(tmp_fd, 'w', encoding='utf-8') as f:
                    json.dump(memory, f, indent=2, ensure_ascii=False)
                # Atomic rename (on same filesystem)
                os.replace(tmp_path, str(MEMORY_PATH))
            except Exception:
                # Clean up temp file on error
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
                raise
        except Exception as e:
            print(f"[Memory] Save error: {e}")


def _truncate_value(val: str) -> str:
    if isinstance(val, str) and len(val) > MAX_VALUE_LENGTH:
        return val[:MAX_VALUE_LENGTH].rstrip() + "…"
    return val


def _recursive_update(target: dict, updates: dict, cat: str | None = None) -> bool:
    changed = False
    for key, value in updates.items():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, dict) and "value" not in value:
            if key not in target or not isinstance(target[key], dict):
                target[key] = {}
                changed = True
            if _recursive_update(target[key], value, cat=key if cat is None else cat):
                changed = True
        else:
            new_val  = _truncate_value(str(value["value"] if isinstance(value, dict) else value))
            existing = target.get(key)
            today    = datetime.now().strftime("%Y-%m-%d")
            if isinstance(existing, dict) and existing.get("value") == new_val:
                # Same fact resurfaced — it matters more, not less.
                existing["freq"]    = existing.get("freq", 1) + 1
                existing["updated"] = today
                changed = True
                continue
            entry = {
                "value":      new_val,
                "updated":    today,
                "freq":       (existing.get("freq", 1) + 1) if isinstance(existing, dict) else 1,
                "importance": CATEGORY_IMPORTANCE.get(cat or "", 2),
            }
            if isinstance(value, dict) and isinstance(value.get("importance"), (int, float)):
                entry["importance"] = max(1, min(5, int(value["importance"])))
            target[key] = entry
            changed = True
    return changed


def update_memory(memory_update: dict) -> dict:
    if not isinstance(memory_update, dict) or not memory_update:
        return load_memory()
    memory = load_memory()
    if _recursive_update(memory, memory_update):
        save_memory(memory)
        print(f"[Memory] Saved: {list(memory_update.keys())}")
        # Count only. The categories written are content, and content does not
        # belong in telemetry.
        try:
            from aoca.events import emit
            emit("memory.written", count=len(memory_update))
        except Exception:
            pass
        _mirror_to_ruflo(memory_update)
    return memory


def _mirror_to_ruflo(memory_update: dict) -> None:
    """Mirror new facts into the ruflo vector DB for semantic recall.
    Fire-and-forget; JSON stays the source of truth."""
    try:
        from memory import ruflo_bridge
        if not ruflo_bridge.available():
            return
        for category, entries in memory_update.items():
            if not isinstance(entries, dict):
                continue
            for key, value in entries.items():
                v = value.get("value") if isinstance(value, dict) else value
                if v:
                    ruflo_bridge.store_async(category, key, str(v))
    except Exception as e:
        print(f"[Memory] ruflo mirror skipped: {e}")


def _sorted_items(memory: dict, cat: str, limit: int) -> list[tuple[str, str]]:
    items = memory.get(cat, {})
    if not isinstance(items, dict):
        return []
    pairs = []
    for key, entry in items.items():
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            pairs.append((key, val, entry if isinstance(entry, dict) else {}))
    pairs.sort(key=lambda p: _score(cat, p[2]), reverse=True)
    return [(k, v) for k, v, _ in pairs[:limit]]


def format_memory_for_prompt(memory: dict | None) -> str:
    if not memory:
        return ""

    lines = []

    identity  = memory.get("identity", {})
    id_fields = ["name", "age", "birthday", "city", "job", "language", "school", "nationality"]
    for field in id_fields:
        entry = identity.get(field)
        if entry:
            val = entry.get("value") if isinstance(entry, dict) else entry
            if val:
                lines.append(f"{field.title()}: {val}")
    for key, entry in identity.items():
        if key in id_fields:
            continue
        val = entry.get("value") if isinstance(entry, dict) else entry
        if val:
            lines.append(f"{key.replace('_', ' ').title()}: {val}")

    sections = [
        ("preferences",   "Preferences:",               20),
        ("projects",      "Active Projects / Goals:",   12),
        ("relationships", "People in their life:",      12),
        ("wishes",        "Wishes / Plans / Wants:",    10),
        ("notes",         "Other notes:",               10),
    ]
    for cat, title, limit in sections:
        items = _sorted_items(memory, cat, limit)
        if items:
            lines.append("")
            lines.append(title)
            for key, val in items:
                lines.append(f"  - {key.replace('_', ' ').title()}: {val}")

    if not lines:
        return ""

    header = "[WHAT YOU KNOW ABOUT THIS PERSON — use naturally, never recite like a list]\n"
    result = header + "\n".join(lines)
    if len(result) > 4000:
        result = result[:3997] + "…"

    return result + "\n"

def remember(key: str, value: str, category: str = "notes") -> str:
    valid = {"identity", "preferences", "projects", "relationships", "wishes", "notes"}
    if category not in valid:
        category = "notes"
    update_memory({category: {key: {"value": value}}})
    return f"Remembered: {category}/{key} = {value}"


def forget(key: str, category: str = "notes") -> str:
    memory = load_memory()
    cat    = memory.get(category, {})
    if key in cat:
        del cat[key]
        memory[category] = cat
        save_memory(memory)
        return f"Forgotten: {category}/{key}"
    return f"Not found: {category}/{key}"


forget_memory = forget


def demo():
    """Self-check on a temp file: freq bump, dedupe, scored trim, formatting."""
    global MEMORY_PATH
    import tempfile, os
    MEMORY_PATH = Path(tempfile.mkdtemp()) / "long_term.json"

    update_memory({"identity": {"name": {"value": "Fatih"}}})
    update_memory({"identity": {"name": {"value": "Fatih"}}})       # same fact again
    m = load_memory()
    assert m["identity"]["name"]["freq"] == 2, m["identity"]["name"]
    assert m["identity"]["name"]["importance"] == 5

    update_memory({"preferences": {"fav_food": {"value": "pizza"},
                                   "favorite_meal": {"value": "Pizza "}}})  # duplicate value
    m = load_memory()
    assert len([k for k, e in m["preferences"].items()
                if str(e.get("value", "")).strip().lower() == "pizza"]) == 1

    out = format_memory_for_prompt(m)
    assert "Fatih" in out and "pizza" in out.lower()

    # trim keeps identity, drops low-score notes
    big = load_memory()
    for i in range(200):
        big["notes"][f"n{i}"] = {"value": "x" * 50, "updated": "2020-01-01",
                                 "freq": 1, "importance": 1}
    trimmed = _trim_to_limit(big)
    assert "name" in trimmed["identity"]
    assert len(json.dumps(trimmed, ensure_ascii=False)) <= MEMORY_MAX_CHARS

    os.remove(MEMORY_PATH)
    print("memory demo OK")


if __name__ == "__main__":
    demo()
