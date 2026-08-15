"""Claude API core — JARVIS's serious-mode brain.

When battle/serious mode is engaged and a Claude API key is configured,
code and reasoning tasks route here instead of Gemini. The adapter mimics
the `.generate_content(prompt).text` interface code_helper already uses,
so switching brains is a one-line decision at the call site.

Key lives in config/api_keys.json:  "claude_api_key": "sk-ant-..."
"""
import json
import sys
from pathlib import Path


def _base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


CONFIG_PATH  = _base_dir() / "config" / "api_keys.json"
CLAUDE_MODEL = "claude-fable-5"   # best coding/reasoning model


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def get_claude_key() -> str:
    return _load_config().get("claude_api_key", "")


def battle_mode_active() -> bool:
    return bool(_load_config().get("battle_mode", False))


def set_battle_mode(enabled: bool) -> None:
    cfg = _load_config()
    cfg["battle_mode"] = bool(enabled)
    CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")


def claude_available() -> bool:
    if not get_claude_key():
        return False
    try:
        import anthropic  # noqa: F401
        return True
    except ImportError:
        return False


class _Response:
    def __init__(self, text: str):
        self.text = text


class ClaudeModel:
    """Drop-in stand-in for google-generativeai's GenerativeModel."""

    def __init__(self, model: str = CLAUDE_MODEL, max_tokens: int = 8096):
        import anthropic
        self._client     = anthropic.Anthropic(api_key=get_claude_key())
        self._model      = model
        self._max_tokens = max_tokens

    def generate_content(self, prompt: str) -> _Response:
        msg = self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            messages=[{"role": "user", "content": str(prompt)}],
        )
        return _Response("".join(b.text for b in msg.content if b.type == "text"))


def get_brain(gemini_factory, model: str | None = None):
    """Serious mode + key present → Claude; otherwise the usual Gemini."""
    if battle_mode_active() and claude_available():
        try:
            return ClaudeModel(model or CLAUDE_MODEL)
        except Exception as e:  # noqa: BLE001
            print(f"[Claude] init failed, falling back to Gemini: {e}")
    return gemini_factory()


def demo():
    """Self-check without network: flag roundtrip + availability logic."""
    was = battle_mode_active()
    set_battle_mode(True);  assert battle_mode_active() is True
    set_battle_mode(False); assert battle_mode_active() is False
    set_battle_mode(was)
    assert isinstance(claude_available(), bool)
    r = _Response("ok"); assert r.text == "ok"
    print("claude_api demo OK")


if __name__ == "__main__":
    demo()
