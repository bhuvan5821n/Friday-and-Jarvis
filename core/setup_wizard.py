"""First-run onboarding for Gemini API credential setup."""
from __future__ import annotations

import json
import os
import sys
import webbrowser
from pathlib import Path


def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent


BASE_DIR = get_base_dir()
CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
ENV_PATH = BASE_DIR / ".env"
GEMINI_KEY_URL = "https://aistudio.google.com/apikey"


def _load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save_config(config: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, indent=4), encoding="utf-8")


def get_gemini_key() -> str:
    """Get Gemini API key from env, .env file, or config/api_keys.json."""
    # 1. Check environment variable
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    
    # 2. Check .env file
    if ENV_PATH.exists():
        try:
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "GEMINI_API_KEY" and v.strip():
                        return v.strip().strip('"').strip("'")
        except Exception:
            pass
    
    # 3. Check config file
    config = _load_config()
    key = config.get("gemini_api_key", "")
    if key:
        return key
    
    return ""


def is_gemini_configured() -> bool:
    """Check if a Gemini API key is configured (not just placeholder)."""
    key = get_gemini_key()
    return bool(key) and key != "your_gemini_api_key_here"


def save_gemini_key(key: str) -> bool:
    """Save Gemini API key to config file and .env file."""
    key = key.strip()
    if not key:
        return False
    
    # Save to .env file
    try:
        lines = []
        if ENV_PATH.exists():
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip().startswith("GEMINI_API_KEY="):
                    lines.append(line)
        lines.append(f"GEMINI_API_KEY={key}")
        ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception as e:
        print(f"[Setup] Warning: Could not write .env file: {e}")
    
    # Also save to config file for backward compatibility
    config = _load_config()
    config["gemini_api_key"] = key
    _save_config(config)
    
    return True


def remove_gemini_key() -> bool:
    """Remove Gemini API key from all config locations."""
    # Remove from .env
    try:
        if ENV_PATH.exists():
            lines = []
            for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
                if not line.strip().startswith("GEMINI_API_KEY="):
                    lines.append(line)
            ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass
    
    # Remove from config
    config = _load_config()
    config["gemini_api_key"] = ""
    _save_config(config)
    
    return True


def test_gemini_connection(api_key: str = None) -> tuple[bool, str]:
    """Test Gemini API connection. Returns (success, message)."""
    if api_key is None:
        api_key = get_gemini_key()
    
    if not api_key or api_key == "your_gemini_api_key_here":
        return False, "No API key configured. Please provide a valid Gemini API key."
    
    try:
        from google import genai
        client = genai.Client(api_key=api_key)
        # Simple test: list models
        models = list(client.models.list())
        if models:
            return True, f"Connected successfully. Found {len(models)} available models."
        else:
            return True, "Connected successfully, but no models found."
    except Exception as e:
        error_msg = str(e)
        if "invalid" in error_msg.lower() or "api_key" in error_msg.lower():
            return False, f"Invalid API key: {error_msg}"
        elif "network" in error_msg.lower() or "connection" in error_msg.lower():
            return False, f"Network error: {error_msg}"
        elif "quota" in error_msg.lower() or "rate" in error_msg.lower():
            return False, f"Quota/rate limit error: {error_msg}"
        else:
            return False, f"Connection error: {error_msg}"


def open_gemini_api_page():
    """Open the Gemini API key page in the default browser."""
    webbrowser.open(GEMINI_KEY_URL)


def run_first_run_setup() -> bool:
    """Interactive first-run setup for Gemini API key.
    
    Returns True if setup completed successfully, False otherwise.
    """
    print("\n" + "=" * 50)
    print("          Welcome to FRIDAY/JARVIS")
    print("=" * 50)
    print("\nFRIDAY needs a Gemini API key for its AI capabilities.")
    print("Use your own Gemini API key.\n")
    print(f"Get your API key at: {GEMINI_KEY_URL}")
    print("-" * 50)
    
    while True:
        api_key = input("\nEnter your Gemini API key (or 'quit' to exit): ").strip()
        
        if api_key.lower() in ('quit', 'exit', 'q'):
            print("\nSetup cancelled. You can run setup again later.")
            return False
        
        if not api_key:
            print("Please enter a valid API key.")
            continue
        
        print("\nTesting connection...")
        success, message = test_gemini_connection(api_key)
        
        if success:
            print(f"✓ {message}")
            save_gemini_key(api_key)
            print("\n✓ API key saved successfully!")
            print("=" * 50)
            return True
        else:
            print(f"✗ {message}")
            retry = input("\nWould you like to try again? (y/n): ").strip().lower()
            if retry != 'y':
                print("\nSetup cancelled. You can run setup again later.")
                return False


if __name__ == "__main__":
    run_first_run_setup()
