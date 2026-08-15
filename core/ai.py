"""OmniRoute — JARVIS's single AI backend.  http://localhost:20128/v1

Every text/vision AI request in JARVIS goes through here. No call site talks
to Gemini/Claude/OpenAI directly anymore — they ask OmniRoute, which routes
to the best of its ~480 models and handles provider failover itself.

JARVIS's job is only to CLASSIFY the task and pick an OmniRoute auto-bucket:

    coding      -> auto/best-coding      (battle: auto/pro-coding)
    reasoning   -> auto/best-reasoning   (battle: auto/pro-reasoning)
    vision      -> auto/best-vision      (battle: auto/pro-vision)
    chat        -> auto/best-chat        (battle: auto/pro-chat)
    fast        -> auto/best-fast        (battle: auto/pro-fast)

Exceptions that stay on Gemini directly (platform capabilities an
OpenAI-compatible endpoint cannot provide):
    - main.py / screen_processor.py  : Live native-audio voice sessions
    - web_search.py                  : Google-Search grounding (has DDG fallback)

Config precedence: .env (OMNIROUTE_BASE_URL / OMNIROUTE_API_KEY)
then config/api_keys.json ("omniroute_base_url" / "omniroute_api_key").

Drop-in interface: OmniModel().generate_content(prompt).text — same shape as
google-generativeai's GenerativeModel, so call sites changed one line each.
Parts lists may mix str, PIL.Image, raw PNG bytes — images auto-route to the
vision bucket.

Manual override ("switch to Claude"): set_override("claude") pins all traffic
to that family until set_override(None). Persisted in config/ai_prefs.json.
"""
from __future__ import annotations

import base64
import io
import json
import sys
import threading
import time
from pathlib import Path

import requests

BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent
PREFS_PATH   = BASE_DIR / "config" / "ai_prefs.json"
KEYS_PATH    = BASE_DIR / "config" / "api_keys.json"
METRICS_PATH = BASE_DIR / "Logs" / "ai_metrics.jsonl"

DEFAULT_BASE_URL = "http://localhost:20128/v1"


def _env() -> dict:
    """Tiny .env reader — stdlib only, no python-dotenv dependency."""
    out = {}
    try:
        for line in (BASE_DIR / ".env").read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return out


def _keys() -> dict:
    try:
        return json.loads(KEYS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def base_url() -> str:
    return (_env().get("OMNIROUTE_BASE_URL")
            or _keys().get("omniroute_base_url")
            or DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    return _env().get("OMNIROUTE_API_KEY") or _keys().get("omniroute_api_key", "")


def _headers() -> dict:
    h = {"Content-Type": "application/json"}
    if api_key():
        h["Authorization"] = f"Bearer {api_key()}"
    return h


# ── routing table ───────────────────────────────────────────────────────

TASKS = {          # task -> (normal bucket, battle bucket)
    "coding":      ("auto/best-coding",      "auto/pro-coding"),
    "coding_fast": ("auto/best-coding-fast", "auto/pro-coding"),
    "reasoning":   ("auto/best-reasoning",   "auto/pro-reasoning"),
    "vision":      ("auto/best-vision",      "auto/pro-vision"),
    "chat":        ("auto/best-chat",        "auto/pro-chat"),
    "fast":        ("auto/best-fast",        "auto/pro-fast"),
}
FALLBACK_CHAIN = ["auto/best-chat", "auto/fast", "auto/cheap", "auto/offline"]

# voice override names -> OmniRoute model families
OVERRIDES = {
    "claude":  "auto/claude-sonnet",
    "claude opus": "auto/claude-opus",
    "gemini":  "auto/gemini",
    "gpt":     "auto/smart",
    "gpt-5":   "auto/smart",
    "deepseek": "auto/reasoning",
    "ollama":  "auto/offline",
    "offline": "auto/offline",
}

_KEYWORDS = {      # cheap keyword classifier — first hit wins
    "coding":    ("code", "function", "debug", "error", "exception", "script",
                  "python", "javascript", "compile", "refactor", "bug", "fix "),
    "reasoning": ("why", "explain", "analyze", "compare", "plan", "solve",
                  "math", "calculate", "prove", "research", "architecture"),
}


def classify(prompt: str) -> str:
    p = prompt[:600].lower()
    for task, words in _KEYWORDS.items():
        if any(w in p for w in words):
            return task
    return "fast" if len(prompt) < 200 else "chat"


def battle_mode() -> bool:
    return bool(_keys().get("battle_mode", False))


# ── manual override (persisted) ─────────────────────────────────────────

def _prefs() -> dict:
    try:
        return json.loads(PREFS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def set_override(name: str | None) -> str:
    """'claude' / 'gemini' / 'gpt' / 'deepseek' / 'ollama' / exact model id / None."""
    prefs = _prefs()
    if not name:
        prefs.pop("override", None)
        msg = "AI routing back to automatic."
    else:
        model = OVERRIDES.get(name.strip().lower(), name.strip())
        prefs["override"] = model
        msg = f"All AI requests pinned to {model}."
    PREFS_PATH.parent.mkdir(exist_ok=True)
    PREFS_PATH.write_text(json.dumps(prefs, indent=2), encoding="utf-8")
    return msg


def get_override() -> str | None:
    return _prefs().get("override")


# ── model resolution ────────────────────────────────────────────────────

def pick_model(task: str = "chat", has_image: bool = False) -> str:
    if get_override():
        return get_override()
    if has_image:
        task = "vision"
    normal, battle = TASKS.get(task, TASKS["chat"])
    return battle if battle_mode() else normal


# ── message building (str | [str, PIL.Image, bytes, ...] -> OpenAI parts) ──

_MAX_IMG_SIDE = 1568   # vision models' sweet spot; full screenshots get rejected


def _image_part(img_or_bytes) -> dict:
    """Downscale + JPEG-compress so multi-MB screenshots don't get dropped."""
    from PIL import Image
    img = (Image.open(io.BytesIO(bytes(img_or_bytes)))
           if isinstance(img_or_bytes, (bytes, bytearray)) else img_or_bytes)
    if max(img.size) > _MAX_IMG_SIDE:
        scale = _MAX_IMG_SIDE / max(img.size)
        img = img.resize((int(img.width * scale), int(img.height * scale)))
    buf = io.BytesIO()
    img.convert("RGB").save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode()
    return {"type": "image_url",
            "image_url": {"url": f"data:image/jpeg;base64,{b64}"}}


def _to_content(parts) -> tuple[list | str, bool]:
    """Returns (OpenAI message content, has_image)."""
    if isinstance(parts, str):
        return parts, False
    if not isinstance(parts, (list, tuple)):
        return str(parts), False
    content, has_image = [], False
    for p in parts:
        if isinstance(p, str):
            content.append({"type": "text", "text": p})
        elif isinstance(p, (bytes, bytearray)) or hasattr(p, "save"):
            has_image = True
            content.append(_image_part(p))
        else:
            content.append({"type": "text", "text": str(p)})
    return content, has_image


def _identity_user_text(parts) -> str:
    """Extract only the current text request for the local identity router."""
    if isinstance(parts, str):
        return parts
    if not isinstance(parts, (list, tuple)):
        return ""
    text_parts = [part for part in parts if isinstance(part, str)]
    if not text_parts:
        return ""
    combined = "\n".join(text_parts)
    marker = "[USER MESSAGE]\n"
    if marker in combined:
        return combined.split(marker, 1)[1].split("\n\n[ATTACHMENT", 1)[0]
    return combined if len(text_parts) == 1 else ""


# ── live status for the AI Control Center UI ────────────────────────────

_STATUS_LOCK = threading.Lock()
_STATUS: dict = {}          # last routing decision + outcome, UI polls this


def _set_status(**kw) -> None:
    with _STATUS_LOCK:
        _STATUS.update(kw)


def status() -> dict:
    """Snapshot of the most recent routing decision (thread-safe copy)."""
    with _STATUS_LOCK:
        return dict(_STATUS)


def history(n: int = 12) -> list[dict]:
    """Last n routing events, newest first, from the metrics log."""
    try:
        lines = METRICS_PATH.read_text(encoding="utf-8").splitlines()
        out = []
        for line in reversed(lines):
            if line.strip():
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
            if len(out) >= n:
                break
        return out
    except Exception:
        return []


def _routing_reason(task: str, model: str, has_image: bool,
                    pinned: str | None) -> tuple[str, int]:
    """Human explanation of the pick + confidence %. Never exposes prompts."""
    if pinned:
        return f"Caller pinned {pinned}", 100
    if get_override():
        return f"Manual override active → {model}", 100
    parts = []
    if has_image:
        parts.append("image attached — vision-capable model required")
    parts.append({
        "coding":      "coding task — strongest code model",
        "coding_fast": "quick code generation — fast code model",
        "reasoning":   "complex reasoning — deep-analysis model",
        "vision":      "screen/image analysis — vision model",
        "chat":        "conversation — balanced chat model",
        "fast":        "simple request — fastest model",
    }.get(task, f"task '{task}'"))
    if battle_mode():
        parts.append("BATTLE MODE — pro tier engaged")
    return "; ".join(parts), (95 if has_image else 92)


def _log_metric(**kw) -> None:
    try:
        METRICS_PATH.parent.mkdir(exist_ok=True)
        kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        with METRICS_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(kw) + "\n")
    except Exception:
        pass


# ── the client ──────────────────────────────────────────────────────────

class _Response:
    def __init__(self, text: str):
        self.text = text


class OmniModel:
    """Drop-in replacement for google-generativeai's GenerativeModel."""

    def __init__(self, task: str = "chat", model: str | None = None,
                 system: str | None = None, timeout: float = 120.0):
        self.task, self.model, self.system, self.timeout = task, model, system, timeout

    def _candidates(self, has_image: bool) -> list[str]:
        first = self.model or pick_model(self.task, has_image)
        chain = [first] + [m for m in FALLBACK_CHAIN if m != first]
        # ponytail: vision fallback still needs vision-capable buckets
        if has_image:
            chain = [first, "auto/vision", "auto/multimodal"]
        return chain

    def generate_content(self, parts, stream_cb=None) -> _Response:
        # Deterministic assistant-identity response before classification,
        # provider selection, or any OmniRoute request.  Chat Studio performs
        # the same explicit entry-point check; this protects other text callers.
        from core.creator_identity import local_creator_response
        identity_text = _identity_user_text(parts)
        local_response = local_creator_response(identity_text) if identity_text else None
        if local_response is not None:
            if stream_cb:
                stream_cb(local_response)
            _set_status(task="identity", reason="local creator identity", confidence=100,
                        state="local", provider="local")
            return _Response(local_response)
        content, has_image = _to_content(parts)
        messages = ([{"role": "system", "content": self.system}] if self.system else []) \
            + [{"role": "user", "content": content}]
        candidates = self._candidates(has_image)
        reason, conf = _routing_reason(self.task, candidates[0], has_image, self.model)
        last_err = None
        for i, model in enumerate(candidates):
            t0 = time.time()
            _set_status(model=model, task=self.task, reason=reason, confidence=conf,
                        fallback_step=i, state="routing",
                        ts=time.strftime("%H:%M:%S"))
            try:
                # OmniRoute always answers in SSE — one path for stream & not
                text, served, provider = self._stream(
                    model, messages, stream_cb or (lambda _: None))
                if not text.strip():
                    raise ValueError("empty response")
                _set_status(state="ok", served=served, provider=provider,
                            latency=round(time.time() - t0, 2))
                return _Response(text)
            except Exception as e:  # noqa: BLE001
                last_err = e
                _log_metric(model=model, task=self.task, ok=False,
                            latency=round(time.time() - t0, 2), error=str(e)[:200])
                _set_status(state="fallback", error=str(e)[:120])
                print(f"[AI] {model} failed ({str(e)[:120]}) - trying next")
        _set_status(state="error", error=str(last_err)[:120])
        raise RuntimeError(f"OmniRoute: all models failed. Last error: {last_err}")

    def _stream(self, model: str, messages: list, cb) -> tuple[str, str, str]:
        """Returns (text, served_model, provider) — served info from headers."""
        t0, chunks = time.time(), []
        chunk_timeout = 30  # Max seconds to wait for next chunk
        # Assigned inside the request; a connection failure never reaches that
        # point, and the metric call below must not raise over the real error.
        served, provider, ok = model, "omniroute", False

        try:
            with requests.post(f"{base_url()}/chat/completions", headers=_headers(),
                               timeout=self.timeout, stream=True,
                               json={"model": model, "messages": messages,
                                     "stream": True}) as r:
                r.raise_for_status()
                served   = r.headers.get("x-omniroute-model", "") or model
                provider = r.headers.get("x-omniroute-provider", "") or "omniroute"
                ok = True
                
                last_chunk_time = time.time()
                for line in r.iter_lines(decode_unicode=True):
                    # Check for chunk timeout
                    if time.time() - last_chunk_time > chunk_timeout:
                        print(f"[AI] Stream timeout: no data for {chunk_timeout}s")
                        break
                    
                    if not line or not line.startswith("data:"):
                        continue
                    payload = line[5:].strip()
                    if payload == "[DONE]":
                        break
                    try:
                        delta = json.loads(payload)["choices"][0]["delta"].get("content")
                    except Exception:
                        continue
                    if delta:
                        chunks.append(delta)
                        cb(delta)
                        last_chunk_time = time.time()
        except requests.exceptions.Timeout:
            print(f"[AI] Request timeout after {self.timeout}s")
        except requests.exceptions.ConnectionError as e:
            print(f"[AI] Connection error: {e}")
        
        _log_metric(model=model, served=served, provider=provider, task=self.task,
                    ok=ok, latency=round(time.time() - t0, 2))
        return "".join(chunks), served, provider


# ── Image Studio backend ────────────────────────────────────────────────

IMAGE_MODELS = [                      # fallback order: fast → quality → alt
    "nvidia/black-forest-labs/flux.1-schnell",
    "nvidia/black-forest-labs/flux.1-dev",
    "nvidia/black-forest-labs/flux.2-klein-4b",
    "zm/openai/gpt-image-2",
]
IMAGES_DIR = BASE_DIR / "Studios" / "Images"


def generate_image(prompt: str, size: str = "1024x1024",
                   model: str | None = None) -> tuple[bytes, Path]:
    """Text → image through OmniRoute /images/generations. Returns
    (image bytes, saved path). Tries each model in IMAGE_MODELS until one
    delivers; handles both b64_json and url response shapes."""
    last_err = None
    for m in ([model] if model else IMAGE_MODELS):
        t0 = time.time()
        try:
            r = requests.post(f"{base_url()}/images/generations",
                              headers=_headers(), timeout=180,
                              json={"model": m, "prompt": prompt,
                                    "n": 1, "size": size})
            r.raise_for_status()
            item = (r.json().get("data") or [{}])[0]
            if item.get("b64_json"):
                raw = base64.b64decode(item["b64_json"])
            elif item.get("url"):
                raw = requests.get(item["url"], timeout=120).content
            else:
                raise ValueError("no image in response")
            if len(raw) < 1000:
                raise ValueError(f"suspiciously small image ({len(raw)}B)")
            IMAGES_DIR.mkdir(parents=True, exist_ok=True)
            ext = ".jpg" if raw[:3] == b"\xff\xd8\xff" else ".png"
            path = IMAGES_DIR / (time.strftime("img_%Y%m%d_%H%M%S") + ext)
            path.write_bytes(raw)
            _log_metric(model=m, task="image", ok=True,
                        latency=round(time.time() - t0, 2), bytes=len(raw))
            _set_status(model=m, served=m, provider="omniroute", task="image",
                        reason="image generation", confidence=95, state="ok",
                        latency=round(time.time() - t0, 2),
                        ts=time.strftime("%H:%M:%S"))
            return raw, path
        except Exception as e:  # noqa: BLE001
            last_err = e
            _log_metric(model=m, task="image", ok=False,
                        latency=round(time.time() - t0, 2), error=str(e)[:200])
            print(f"[AI] image model {m} failed ({str(e)[:100]}) - trying next")
    raise RuntimeError(f"Image generation failed on all models: {last_err}")


def ask(prompt: str, task: str | None = None, system: str | None = None) -> str:
    """One-shot convenience: auto-classified unless task given."""
    return OmniModel(task or classify(prompt), system=system).generate_content(prompt).text


def available(timeout: float = 2.0) -> bool:
    try:
        return requests.get(f"{base_url()}/models",
                            headers=_headers(), timeout=timeout).ok
    except Exception:
        return False


def demo():
    """Offline-safe self-checks + one live round-trip if OmniRoute is up."""
    assert classify("fix this python error in my code") == "coding"
    assert classify("why does inflation happen, explain") == "reasoning"
    assert classify("hi") == "fast"
    assert pick_model("coding") in ("auto/best-coding", "auto/pro-coding")
    assert pick_model("chat", has_image=True).endswith("vision")
    was = get_override()
    set_override("claude"); assert get_override() == "auto/claude-sonnet"
    set_override(None);     assert get_override() is None
    if was:
        set_override(was)
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", (2000, 500), "red").save(buf, format="PNG")
    content, has_img = _to_content(["hello", buf.getvalue()])
    assert "jpeg" in content[1]["image_url"]["url"][:22], "large image must be shrunk+jpeg"
    assert has_img and content[0]["type"] == "text" and content[1]["type"] == "image_url"
    print("ai demo OK (offline checks)")
    if available():
        t = ask("Reply with exactly: PONG", task="fast")
        print(f"live round-trip OK: {t[:60]!r}")
    else:
        print("OmniRoute not reachable - live check skipped")


if __name__ == "__main__":
    demo()
