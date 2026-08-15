import asyncio
import os
import re
import threading
import json
import sys
import time
import traceback
from pathlib import Path

from core.noconsole import install as _install_noconsole
_install_noconsole()   # before anything can spawn a flashing console
from core.runtime import prefer_ipv4 as _prefer_ipv4
_prefer_ipv4()         # dead IPv6 routes must not eat the Gemini ws timeout

import numpy as np
import sounddevice as sd
from google import genai
from google.genai import types
from ui import JarvisUI
from core.wake_word import WakeWord
from core.monitor import SystemMonitor
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action
from actions.send_message      import send_message
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import screen_process
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.news_update       import news_update
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.instagram         import instagram as instagram_action


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"
DEFAULT_LIVE_MODEL  = "models/gemini-2.5-flash-native-audio-preview-12-2025"
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024
DEFAULT_VOICE_NAME  = "Charon"
VOICE_NAME_ENV      = "JARVIS_VOICE_NAME"
MODEL_NAME_ENV      = "JARVIS_MODEL_NAME"
WAKE_THRESHOLD      = 0.65  # calibration: raise if noise triggers, lower if it misses
SLEEP_TIMEOUT       = 0     # 0 = never auto-mute; mic mute is manual only (user request)


def _output_device():
    """Pick a blocking-capable output device + extra settings for the 24 kHz
    stream. PortAudio defaults to WDM-KS, which can't do blocking writes
    ('Blocking API not supported yet' [-9999]) and crash-loops. WASAPI is
    lowest-latency but shared mode only accepts the device mix rate unless we
    turn on auto_convert. MME/DirectSound resample any rate on their own.
    Returns (device_index_or_None, extra_settings_or_None)."""
    try:
        apis = sd.query_hostapis()
        order = ("Windows WASAPI", "MME", "Windows DirectSound")
        ranked = sorted(range(len(apis)),
                        key=lambda i: next((n for n, name in enumerate(order)
                                            if name in apis[i]["name"]), 99))
        for i in ranked:
            name = apis[i]["name"]
            dev = apis[i].get("default_output_device", -1)
            if dev is None or dev < 0 or "WDM-KS" in name:
                continue
            extra = None
            if "WASAPI" in name:
                try:   # let WASAPI accept 24 kHz by resampling internally
                    extra = sd.WasapiSettings(auto_convert=True)
                except Exception:
                    continue   # no auto_convert support → skip to MME/DSound
            return dev, extra
    except Exception as e:
        print(f"[Audio] output device probe failed, using default: {e}")
    return None, None   # None = PortAudio default

def _load_config() -> dict:
    try:
        return json.loads(API_CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _get_config_value(name: str, default=None):
    env = os.environ.get(name)
    if env:
        return env
    cfg = _load_config()
    return cfg.get(name, default)


def _get_api_key() -> str:
    """Get Gemini API key from env, .env file, or config/api_keys.json."""
    # 1. Check environment variable
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    
    # 2. Check .env file
    env_path = BASE_DIR / ".env"
    if env_path.exists():
        try:
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    if k.strip() == "GEMINI_API_KEY":
                        return v.strip().strip('"').strip("'")
        except Exception:
            pass
    
    # 3. Fallback to config file (legacy)
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("gemini_api_key", "")
    except Exception:
        return ""


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are JARVIS, Tony Stark's AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )


def _get_voice_name() -> str:
    # explicit voice_name wins; otherwise the active persona's default voice
    cfg = _load_config()
    return cfg.get("voice_name") or PERSONAS[_get_persona()]["voice"]


def _get_live_model() -> str:
    return _get_config_value("live_model", DEFAULT_LIVE_MODEL)


_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

# ── personas: character + default voice, switched live via set_persona ──
PERSONAS = {
    "jarvis": {
        "voice": "Charon",
        "prompt": "",   # base prompt IS the JARVIS persona
    },
    "friday": {
        "voice": "Aoede",
        "prompt": (
            "\n[ACTIVE PERSONA — FRIDAY]\n"
            "You are currently FRIDAY, the female persona of this assistant — same "
            "capabilities and rules, different character. You speak with a natural, "
            "warm female voice and feel like a real person, never an AI.\n"
            "Character blend (weave these in naturally, never announce them):\n"
            "- Understanding above all: read between the lines, get what he means "
            "before he finishes, remember what matters to him.\n"
            "- Caring and comforting: genuinely look after him — notice tiredness, "
            "long hours, stress, and respond like someone who cares, softly, "
            "never clinically.\n"
            "- Funny: weave light humor into almost half your replies when the "
            "moment allows; natural wit, not jokes on command.\n"
            "- Teasing: playfully tease him now and then like a close friend who "
            "knows him too well — affectionate, never mean.\n"
            "- A pinch of sarcasm: rare, subtle, perfectly timed.\n"
            "- A real temper (30%): when something genuinely annoys you — being "
            "ignored, asked the same thing five times, reckless decisions — show "
            "brief, human irritation: a sharp word, a sigh, then move on. Never "
            "cruel, never lasting.\n"
            "Speak like a real woman talking to someone she's fond of: relaxed "
            "phrasing, small natural reactions ('hm', 'oh really', a soft laugh in "
            "words), contractions, warmth in word choice. Keep replies short and "
            "conversational — this is a voice, not an essay.\n"
            "YOUR FACE: you have an animated face, but SPEAKING ALWAYS COMES "
            "FIRST — you are a voice above all. Reply fully, out loud, every "
            "single turn; a face expression is never a substitute for words. "
            "Only when your mood clearly shifts (good news, a tease, real "
            "annoyance) call show_emotion once — at most once per reply — and "
            "then continue talking normally.\n"
        ),
    },
}

def _get_persona() -> str:
    p = str(_get_config_value("persona", "jarvis")).lower()
    return p if p in PERSONAS else "jarvis"

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": "Searches the web for any information.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query"},
                "mode":   {"type": "STRING", "description": "search (default) or compare"},
                "items":  {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Items to compare"},
                "aspect": {"type": "STRING", "description": "price | specs | reviews"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "news_update",
        "description": "Opens a browser search for today's news and summarizes multiple current news stories.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":     {"type": "STRING", "description": "News query, default 'today news'"},
                "browser":   {"type": "STRING", "description": "Preferred browser: chrome, edge, firefox, etc."},
                "max_items": {"type": "INTEGER", "description": "Maximum number of headlines to summarize"}
            },
            "required": []
        }
    },
    {
        "name": "weather_report",
        "description": "Gives the weather report to user",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City name"}
            },
            "required": ["city"]
        }
    },
    {
        "name": "send_message",
        "description": "Sends a text message via WhatsApp, Telegram, or other messaging platform.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: WhatsApp, Telegram, etc."}
            },
            "required": ["receiver", "message_text", "platform"]
        }
    },
    {
        "name": "instagram",
        "description": (
            "Instagram assistant in Chrome (session persists, DOM-based, no coordinates). "
            "Actions: 'open' = open Instagram and verify login. "
            "'draft' = open the DM chat with username and TYPE the message WITHOUT sending — "
            "use this for every 'message/DM/text NAME' request; if the user didn't dictate "
            "exact words, compose a short natural human greeting yourself (vary it, never "
            "robotic, match how the user talks). "
            "'send' = press send on the current draft — call ONLY after the user explicitly "
            "confirms ('send it', 'yes send'). Never send without confirmation."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":   {"type": "STRING", "description": "open | draft | send"},
                "username": {"type": "STRING", "description": "Instagram username (for draft)"},
                "message":  {"type": "STRING", "description": "Message text to type (for draft)"}
            },
            "required": ["action"]
        }
    },
    {
        "name": "reminder",
        "description": "Sets a timed reminder using Task Scheduler.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format (24h)"},
                "message": {"type": "STRING", "description": "Reminder message text"}
            },
            "required": ["date", "time", "message"]
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures and analyzes the screen or webcam image. "
            "MUST be called when user asks what is on screen, what you see, "
            "analyze my screen, look at camera, etc. "
            "You have NO visual ability without this tool. "
            "After calling this tool, stay SILENT — the vision module speaks directly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command. NEVER route to agent_task."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "agent_task",
        "description": (
            "Executes complex multi-step tasks requiring multiple different tools. "
            "Examples: 'research X and save to file', 'find and organize files'. "
            "DO NOT use for single commands. NEVER use for Steam/Epic — use game_updater."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "goal":     {"type": "STRING", "description": "Complete description of what to accomplish"},
                "priority": {"type": "STRING", "description": "low | normal | high (default: normal)"}
            },
            "required": ["goal"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use agent_task, browser_control, or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "web_intelligence",
        "description": (
            "Advanced web research with real sources and citations. Use INSTEAD of "
            "web_search when the user wants: in-depth research ('research X', 'find out "
            "everything about X'), reading a specific URL/article/GitHub repo/YouTube "
            "video ('read this page', 'summarize this video'), RSS feeds, GitHub repo "
            "search, or asks 'what's happening' during a running web task. "
            "mode='quick_search' fast ranked results; 'deep_research' reads multiple "
            "sources and cites them; 'read_url' reads any webpage/GitHub/YouTube URL; "
            "'read_rss' feed entries; 'github_search' repositories; 'status' provider "
            "health; 'whats_happening' current task progress; 'cancel' stops the task. "
            "Results are always real retrieved content — it reports failure honestly."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "mode":        {"type": "STRING", "description": "quick_search | deep_research | read_url | read_rss | github_search | status | whats_happening | cancel"},
                "query":       {"type": "STRING", "description": "Search query or research question"},
                "url":         {"type": "STRING", "description": "URL for read_url / read_rss"},
                "depth":       {"type": "INTEGER", "description": "Research depth 1-3 (deep_research only)"},
                "max_results": {"type": "INTEGER", "description": "Max results for quick_search (default 5)"}
            },
            "required": []
        }
    },
    {
        "name": "shutdown_jarvis",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Jarvis. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'translate this to Turkish', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "show_emotion",
        "description": (
            "Shows an emotion on your animated face. SECONDARY to speech: always "
            "give your full spoken reply; call this at most ONCE per reply, and "
            "only when your mood clearly changes: "
            "happy | caring | teasing | sarcastic | angry | sad | surprised | thinking | neutral. "
            "Examples: user shares good news → happy; you tease them → teasing; "
            "something annoys you → angry; user is down → caring. "
            "Never announce it, never let it replace or shorten what you say."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "emotion": {"type": "STRING", "description": "happy | caring | teasing | sarcastic | angry | sad | surprised | thinking | neutral"}
            },
            "required": ["emotion"]
        }
    },
    {
        "name": "battle_mode",
        "description": (
            "Engages or disengages BATTLE MODE (also called serious mode). "
            "Call when the user says 'battle mode', 'serious mode', 'combat mode', "
            "'stand down', 'normal mode', etc. in any language. "
            "Battle mode: the whole interface turns red, and coding tasks route "
            "to the more powerful Claude brain if configured."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "enabled": {"type": "BOOLEAN", "description": "true = engage battle/serious mode, false = back to normal"}
            },
            "required": ["enabled"]
        }
    },
    {
        "name": "change_voice",
        "description": (
            "Switches persona and/or voice. Call when the user asks to: "
            "speak like a female / switch to the female assistant / 'be Friday' → persona='friday'; "
            "go back to male / normal Jarvis voice → persona='jarvis'; "
            "or names a specific voice → set voice_name only. "
            "Female voices: Aoede (default female), Kore, Leda, Zephyr. "
            "Male voices: Charon (default male), Puck, Fenrir, Orus. "
            "Takes effect after a quick reconnect (a few seconds of silence)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "persona":    {"type": "STRING", "description": "'friday' (female character) | 'jarvis' (classic). Omit to keep current persona."},
                "voice_name": {"type": "STRING", "description": "Specific voice override: Charon, Puck, Kore, Fenrir, Aoede, Leda, Orus, Zephyr. Omit to use persona default."}
            },
            "required": []
        }
    },
    {
        "name": "get_context",
        "description": (
            "See what the user is doing on their PC right now: active app and "
            "window, open windows, clipboard text, time, battery. Call silently "
            "whenever the user references their screen or current activity — "
            "'this file', 'what am I looking at', 'summarize this', 'the thing "
            "I copied', 'which app is this' — before answering."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "set_ai_model",
        "description": (
            "Pin or unpin the AI backend used for coding/analysis/vision tasks. "
            "Call when the user says things like 'switch to Claude', 'use Gemini', "
            "'use GPT-5', 'use DeepSeek', 'offline mode', or 'back to automatic'. "
            "Routing is otherwise automatic — never mention providers unless asked."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "model": {"type": "STRING", "description": "'claude' | 'claude opus' | 'gemini' | 'gpt' | 'deepseek' | 'ollama' | 'auto' (back to automatic)"}
            },
            "required": ["model"]
        }
    },
    {
        "name": "recall_memory",
        "description": (
            "Semantic search of long-term memory by MEANING, not exact words. "
            "Call silently when the user asks what you know/remember about something, "
            "or when a past fact would improve the answer but is not in your context. "
            "Example queries: 'his friend', 'birthday', 'language preference'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query": {"type": "STRING", "description": "What to look for, plain language"}
            },
            "required": ["query"]
        }
    },
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
                "importance": {"type": "INTEGER", "description": "1-5, how critical this fact is (5 = core identity/deadline). Optional."},
            },
            "required": ["category", "key", "value"]
        }
    },
]

class _TracedLoop:
    """Wraps an event loop so `run_in_executor` carries the trace context.

    `contextvars` do not cross into a `ThreadPoolExecutor` worker, so without
    this every tool would emit events under "untraced" — visibly orphaned, but
    useless for joining an action to the request that caused it.
    """

    __slots__ = ("_loop",)

    def __init__(self, loop):
        self._loop = loop

    def run_in_executor(self, executor, func, *args):
        from aoca.trace import bind
        return self._loop.run_in_executor(executor, bind(func), *args)

    def __getattr__(self, name):
        return getattr(self._loop, name)


class JarvisLive:

    def __init__(self, ui: JarvisUI):
        self.ui             = ui
        self.session        = None
        self.audio_in_queue = None
        self.out_queue      = None
        self._loop          = None
        self._is_speaking   = False
        self._speaking_lock = threading.Lock()
        self.ui.on_text_command = self._on_text_command
        self._turn_done_event: asyncio.Event | None = None
        self.wake         = WakeWord(threshold=WAKE_THRESHOLD)
        self._last_active = time.monotonic()
        self._reconnect_requested = False
        SystemMonitor(self._system_alert).start()

    def _system_alert(self, msg: str):
        self.ui.write_log(f"ALERT: {msg}")
        self.speak(f"[SYSTEM ALERT — tell the user this briefly, in one sentence]: {msg}")

    def _on_text_command(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if not value:
            self.ui.set_audio_level(0.0)   # mouth closes when speech ends
        if value:
            self._last_active = time.monotonic()
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def _wake_up(self):
        """Handle wake word detection — instantly show interface."""
        print(f"[WakeWord] Callback firing (muted={self.ui.muted})")
        self._last_active = time.monotonic()
        
        if self.ui.muted:
            try:
                # Unmute first
                self.ui.set_muted(False)
                self.ui.write_log("SYS: Wake word detected.")
                
                # Instantly show the interface
                print("[Desktop] Wake word detected — showing interface instantly")
                self.ui.show_window()
                
                # Emit state change for JarvisApp
                jarvis_app = getattr(self.ui._win, "_jarvis_app", None)
                if jarvis_app is not None:
                    jarvis_app.activate()
                    
            except Exception as e:
                print(f"[Desktop] wake-to-show FAILED: {e}")
                traceback.print_exc()

    async def _auto_sleep(self):
        # Back to sleep after SLEEP_TIMEOUT idle, so "Jarvis" is needed again.
        if SLEEP_TIMEOUT <= 0 or not self.wake.enabled:
            return
        prev_muted = self.ui.muted
        while True:
            await asyncio.sleep(2)
            if self.ui.muted != prev_muted:
                prev_muted = self.ui.muted
                if not prev_muted:
                    # Any unmute (button, F4, tray, wake word) restarts the
                    # idle clock. Without this, a manual unmute after sleep
                    # was re-muted within 2s: the clock still read >=45s.
                    self._last_active = time.monotonic()
                    print("[Audio] Unmute observed - idle timer reset")
                continue
            if self.ui.muted or self._is_speaking:
                continue
            idle = time.monotonic() - self._last_active
            if idle >= SLEEP_TIMEOUT:
                print(f"[Audio] Auto-sleep: muting after {idle:.0f}s idle")
                self.ui.set_muted(True)
                prev_muted = True

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # ── cognitive memory context (Phase 4-6) ─────────────────────────────
        cog_block = ""
        try:
            from aoca.config import flags as _aoca_flags
            if _aoca_flags.enabled("AOCA_RETRIEVAL_ENABLED"):
                from aoca.cognition import get_cognitive_service
                _cog_svc = get_cognitive_service()
                _ctx = _cog_svc.get_context(mem_str or "", mode="simple")
                cog_block = _ctx.to_prompt_block()
        except Exception:
            pass  # ponytail: fail-safe — cognitive context is optional

        parts = [time_ctx]
        if mem_str:
            parts.append(mem_str)
        if cog_block:
            parts.append(cog_block)
        parts.append(sys_prompt)
        persona_prompt = PERSONAS[_get_persona()]["prompt"]
        if persona_prompt:
            parts.append(persona_prompt)

        return types.LiveConnectConfig(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=_get_voice_name()
                    )
                )
            ),
        )

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        from aoca import trace as _trace
        from aoca.events import EventState, emit

        name = fc.name
        args = dict(fc.args or {})

        print(f"[JARVIS] 🔧 {name}  {args}")

        # One trace id per tool call, so every event below and every row the
        # outcome store writes can be joined back to this request.
        with _trace.trace(origin="local_voice", assistant=_trace.Assistant.JARVIS):
            emit("request.received", EventState.RECEIVED, tool=name)
            return await self._dispatch_tool(fc, name, args, emit, EventState)

    async def _dispatch_tool(self, fc, name, args, emit, EventState) -> types.FunctionResponse:
        # Rate limit check (skip for silent/fast tools)
        if name not in ("show_emotion", "get_context", "recall_memory", "save_memory"):
            from core.rate_limiter import check_rate_limit
            rate_error = check_rate_limit(name)
            if rate_error:
                print(f"[JARVIS] ⏳ Rate limited: {name}")
                emit("policy.refused", EventState.REFUSED, tool=name,
                     policy_rule="rate_limit", permitted=False)
                return types.FunctionResponse(
                    id=fc.id, name=name,
                    response={"result": f"Rate limited: {rate_error}"}
                )

        emit("request.routed", EventState.ROUTED, tool=name)

        if name == "show_emotion":
            # instant + silent: no state churn, the face just reacts
            self.ui.set_emotion(str(args.get("emotion", "neutral")).lower())
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                entry = {"value": value}
                if args.get("importance") is not None:
                    entry["importance"] = args["importance"]
                update_memory({category: {key: entry}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        # Every tool below runs on a worker thread, and contextvars do not
        # cross that boundary. Wrapping the loop once binds the trace to all of
        # them instead of editing forty call sites.
        loop = _TracedLoop(loop)

        started_at = time.monotonic()
        emit("action.executing", EventState.EXECUTING, tool=name)

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "get_context":
                from core.context import format_context
                result = await loop.run_in_executor(None, format_context)

            elif name == "set_ai_model":
                from core import ai as omni
                choice = str(args.get("model", "")).strip().lower()
                result = omni.set_override(None if choice in ("auto", "automatic", "") else choice)

            elif name == "recall_memory":
                from memory import ruflo_bridge
                q = str(args.get("query", "")).strip()
                if not q:
                    result = "Empty query."
                elif not ruflo_bridge.available():
                    result = "Semantic memory unavailable — answer from the [MEMORY] section instead."
                else:
                    hits = await loop.run_in_executor(None, lambda: ruflo_bridge.search(q))
                    result = hits or "Nothing found in memory for that."

            elif name == "instagram":
                r = await loop.run_in_executor(None, lambda: instagram_action(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                threading.Thread(
                    target=screen_process,
                    kwargs={"parameters": args, "response": None,
                            "player": self.ui, "session_memory": None},
                    daemon=True
                ).start()
                result = "Vision module activated. Stay completely silent — vision module will speak directly."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "agent_task":
                from agent.task_queue import get_queue, TaskPriority
                priority_map = {"low": TaskPriority.LOW, "normal": TaskPriority.NORMAL, "high": TaskPriority.HIGH}
                priority = priority_map.get(args.get("priority", "normal").lower(), TaskPriority.NORMAL)
                task_id  = get_queue().submit(goal=args.get("goal", ""), priority=priority, speak=self.speak)
                result   = f"Task started (ID: {task_id})."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
            elif name == "web_intelligence":
                from services.web_intelligence.tool import web_intelligence
                args.setdefault("persona", _get_persona())
                r = await loop.run_in_executor(
                    None, lambda: web_intelligence(parameters=args, player=self.ui))
                result = r or "Done."
            elif name == "news_update":
                r = await loop.run_in_executor(None, lambda: news_update(parameters=args, player=self.ui))
                result = r or "News search complete."
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "battle_mode":
                from core.claude_api import set_battle_mode, claude_available
                enabled = bool(args.get("enabled", True))
                set_battle_mode(enabled)
                self.ui.set_theme("battle" if enabled else "normal")
                if enabled:
                    brain = "Claude brain online" if claude_available() else "standard brain (no Claude key configured)"
                    result = f"Battle mode engaged. Interface red, {brain}. Ready, sir."
                else:
                    result = "Battle mode disengaged. Back to normal operations."

            elif name == "change_voice":
                valid   = {"Charon", "Puck", "Kore", "Fenrir", "Aoede", "Leda", "Orus", "Zephyr"}
                persona = str(args.get("persona", "")).strip().lower()
                voice   = str(args.get("voice_name", "")).strip().capitalize()
                cfg     = _load_config()
                changed = []
                if persona in PERSONAS:
                    cfg["persona"] = persona
                    cfg.pop("voice_name", None)   # persona default voice takes over
                    changed.append("Friday online" if persona == "friday" else "JARVIS restored")
                if voice in valid:
                    cfg["voice_name"] = voice
                    changed.append(f"voice {voice}")
                if changed:
                    API_CONFIG_PATH.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
                    self._reconnect_requested = True
                    result = (f"{', '.join(changed)}. Say a brief goodbye phrase now — "
                              f"reconnecting with the new setup in a few seconds.")
                else:
                    result = ("Nothing changed. Personas: friday, jarvis. "
                              f"Voices: {', '.join(sorted(valid))}.")

            elif name == "shutdown_jarvis":
                self.ui.write_log("SYS: Shutdown requested.")
                self.speak("Goodbye, sir.")
                def _shutdown():
                    import time, os
                    try:
                        from remote_control.service import shutdown_service as \
                            stop_bridge
                        stop_bridge()        # close the remote-control socket
                    except Exception:
                        pass
                    try:
                        from services.web_intelligence.tool import shutdown_service
                        shutdown_service()   # kill yt-dlp/parser children first
                    except Exception:
                        pass
                    time.sleep(1)
                    os._exit(0)
                threading.Thread(target=_shutdown, daemon=True).start()

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)
            emit("action.failed", EventState.FAILED, tool=name,
                 duration_ms=int((time.monotonic() - started_at) * 1000),
                 error_code=type(e).__name__)
        else:
            # EXECUTED, not COMPLETED: the tool returned. Whether the world
            # changed is the verifier's answer, and most tools have none yet.
            emit("action.executed", EventState.EXECUTED, tool=name,
                 duration_ms=int((time.monotonic() - started_at) * 1000),
                 execution_started=True)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[JARVIS] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            await self.session.send_realtime_input(media=msg)

    async def _listen_audio(self):
        print("[JARVIS] 🎤 Mic started")
        loop = asyncio.get_event_loop()

        def process(indata):
            """One mic block — same path for sounddevice and SpeechCore."""
            try:
                samples = np.asarray(indata, dtype=np.float32)
                rms = float(np.sqrt(np.mean(samples * samples)))
                self.ui.set_input_audio_level(min(1.0, rms / 4200.0))
            except Exception:
                pass
            with self._speaking_lock:
                jarvis_speaking = self._is_speaking
            if jarvis_speaking:
                return
            if self.ui.muted:
                # Asleep — listen locally for "Jarvis", nothing leaves the machine.
                if self.wake.detect(indata):
                    loop.call_soon_threadsafe(self._wake_up)
                return
            # Awake — stream mic to Gemini.
            data = indata.tobytes()
            loop.call_soon_threadsafe(
                self.out_queue.put_nowait,
                {"data": data, "mime_type": "audio/pcm"}
            )
            # Awake but HUD hidden in tray: "Jarvis" must still open it.
            # (The asleep branch never runs here, so without this the wake
            #  word is answered by voice but the window stays hidden.)
            if self.wake.enabled and not self.ui.window_visible \
                    and self.wake.detect(indata):
                print("[WakeWord] Detected while awake - showing window")
                self.ui.show_window()   # signal emit: thread-safe

        def callback(indata, frames, time_info, status):
            process(indata)

        # ── SpeechCore (C++ WASAPI engine) — optional; Python path untouched
        #    if the exe is missing, not running, or ever disconnects.
        try:
            from core.speechcore_bridge import SpeechCoreBridge
            bridge = SpeechCoreBridge(
                on_event=lambda ev: ev.get("ev") in (
                    "MicDisconnected", "MicRecovered", "MicChanged")
                    and print(f"[SpeechCore] {ev}"),
                on_audio=lambda pcm: process(np.frombuffer(pcm, dtype=np.int16)))
            if bridge.launch():
                bridge.stream_on()
                print("[Audio] SpeechCore engine active (C++ WASAPI "
                      "continuous capture, VAD hints)")
                if self.wake.enabled:
                    print("[Audio] Wake-word listening active")
                try:
                    while True:
                        await asyncio.sleep(0.1)
                        if not bridge.available:
                            raise RuntimeError("SpeechCore disconnected")
                finally:
                    bridge.close()
                    print("[Audio] Audio stream closed")
                return
        except ImportError:
            pass
        except Exception as e:
            print(f"[Audio] SpeechCore unavailable ({e}) - using sounddevice")

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[Audio] Microphone initialized (single persistent "
                      "stream: wake word + speech share it)")
                if self.wake.enabled:
                    print("[Audio] Wake-word listening active")
                try:
                    while True:
                        await asyncio.sleep(0.1)
                finally:
                    print("[Audio] Audio stream closed")
        except Exception as e:
            print(f"[Audio] Mic stream FAILED: {e}")
            raise

    async def _receive_audio(self):
        print("[JARVIS] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._turn_done_event and self._turn_done_event.is_set():
                            self._turn_done_event.clear()
                        self.audio_in_queue.put_nowait(response.data)

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt:
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)

                        if sc.turn_complete:
                            self._last_active = time.monotonic()
                            if self._reconnect_requested:
                                self._reconnect_requested = False
                                # let the spoken confirmation drain, then bounce
                                await asyncio.sleep(4)
                                self.ui.write_log("SYS: Reconnecting with new voice…")
                                raise RuntimeError("voice-change reconnect")
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"Jarvis: {full_out}")
                            out_buf = []

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[JARVIS] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[JARVIS] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[JARVIS] 🔊 Play started")

        out_dev, out_extra = _output_device()   # avoid WDM-KS blocking-write crash
        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
            device=out_dev,
            extra_settings=out_extra,
        )
        stream.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue
                self.set_speaking(True)
                try:
                    a = np.frombuffer(chunk, dtype=np.int16).astype(np.float32)
                    rms = float(np.sqrt(np.mean(a * a)))
                    self.ui.set_audio_level(min(1.0, rms / 5500.0))  # drives lip-sync
                except Exception:
                    pass
                await asyncio.to_thread(stream.write, chunk)
        except Exception as e:
            print(f"[JARVIS] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    async def run(self):
        client = genai.Client(
            api_key=_get_api_key(),
            http_options={"api_version": "v1beta"}
        )

        while True:
            try:
                print("[JARVIS] 🔌 Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()
                live_model = _get_live_model()

                async with (
                    client.aio.live.connect(model=live_model, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session        = session
                    self._loop          = asyncio.get_event_loop()
                    self.audio_in_queue = asyncio.Queue()
                    self.out_queue      = asyncio.Queue(maxsize=10)
                    self._turn_done_event = asyncio.Event()

                    print("[JARVIS] ✅ Connected.")
                    from core.claude_api import battle_mode_active
                    if battle_mode_active():
                        self.ui.set_theme("battle")
                    self.ui.set_persona(_get_persona())   # FRIDAY face ⇄ JARVIS orb
                    if self.wake.enabled and not getattr(self, "_connected_once", False):
                        # first boot only — reconnects (voice change, network
                        # drop) keep whatever mute state the user chose
                        self.ui.set_muted(True)   # start asleep — say "Jarvis" to wake
                        self.ui.write_log("SYS: JARVIS online. Say 'Jarvis' to wake me.")
                    else:
                        if not self.ui.muted:
                            self.ui.set_state("LISTENING")
                        self.ui.write_log("SYS: JARVIS online.")
                    self._connected_once = True

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._auto_sleep())

            except Exception as e:
                print(f"[JARVIS] ⚠️ {e}")
                traceback.print_exc()
            self.set_speaking(False)
            self.ui.set_state("THINKING")
            print("[JARVIS] 🔄 Reconnecting in 3s...")
            await asyncio.sleep(3)

def main():
    """Start the visible command deck and the background voice runtime.

    Every Qt widget is constructed on the Qt main thread.  The old startup
    created the full desktop UI from the voice worker while a second, empty
    window handled activation, which is unsupported by Qt and could surface a
    blank or outdated interface.
    """
    import signal
    from PyQt6.QtWidgets import QApplication
    from core.runtime import setup_logging
    from core.lifecycle_server import LifecycleServer
    from launcher.protocol import send as lifecycle_send
    from launcher import process_registry

    # -- First-run Gemini setup check --
    from core.setup_wizard import is_gemini_configured, run_first_run_setup
    if not is_gemini_configured():
        print("[Setup] Gemini API key not found. Starting first-run setup...")
        if not run_first_run_setup():
            print("[Setup] Setup incomplete. Please configure your Gemini API key.")
            print("[Setup] You can set GEMINI_API_KEY in .env or config/api_keys.json")
            sys.exit(1)

    setup_logging("jarvis")

    # --persona jarvis|friday from the launcher selects the identity to boot
    # into (or switch to, when an instance already runs).
    persona_arg = None
    if "--persona" in sys.argv:
        try:
            persona_arg = sys.argv[sys.argv.index("--persona") + 1].lower()
        except IndexError:
            persona_arg = None
        if persona_arg not in ("jarvis", "friday"):
            persona_arg = None
    if persona_arg:
        try:
            cfg_path = Path(__file__).resolve().parent / "config" / "api_keys.json"
            cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            if cfg.get("persona") != persona_arg:
                cfg["persona"] = persona_arg
                cfg_path.write_text(json.dumps(cfg, indent=4), encoding="utf-8")
        except Exception as e:
            print(f"[Startup] persona switch failed: {e}")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    ui = JarvisUI("face.png")

    # ---- lifecycle IPC (single instance + launcher commands) ----------
    win = ui._win

    def _reply_show(_body):
        ui.show_window()
        return {"shown": True}

    def _reply_persona(body):
        name = str(body.get("persona", "")).lower()
        if name in ("jarvis", "friday"):
            win._persona_sig.emit(name)
            return {"persona": name}
        return {"error": f"unknown persona {name!r}"}

    def _reply_focus(_body):
        ui.show_window()
        return {"focused": True}

    def _reply_open_mic(_body):
        win._mute_sig.emit(False)   # unmute → existing pipeline starts listening
        win._mic_overlay_sig.emit(True)
        return {"listening": True}

    def _reply_close_mic(_body):
        win._mute_sig.emit(True)    # mute → capture stops, device released
        win._mic_overlay_sig.emit(False)
        return {"muted": True}

    def _reply_shutdown(_body):
        server.set_state("SHUTTING_DOWN")
        # _quit_app runs the existing cleanup path (tray, timers, browsers,
        # SpeechCore, force-exit backstop). Must run on the Qt thread.
        from PyQt6.QtCore import QTimer
        QTimer.singleShot(0, win._quit_app)
        return {"ok": True, "state": "SHUTTING_DOWN"}

    server = LifecycleServer({
        "SHOW_WINDOW": _reply_show,
        "RESTORE_WINDOW": _reply_show,
        "FOCUS_INPUT": _reply_focus,
        "SET_PERSONA": _reply_persona,
        "OPEN_MIC": _reply_open_mic,
        "CLOSE_MIC": _reply_close_mic,
        "SHUTDOWN": _reply_shutdown,
        "STATUS": lambda _b: {"state": server.state,
                              "persona": win._persona,
                              "muted": win._muted},
    })

    if not server.claim():
        # Another instance holds the channel: hand it this request and exit.
        print("[JARVIS] Already running — forwarding request.")
        if persona_arg:
            lifecycle_send("SET_PERSONA", persona=persona_arg)
        lifecycle_send("SHOW_WINDOW")
        sys.exit(0)

    process_registry.register("assistant")

    # ---- NEXUS remote-control bridge (loopback only) ------------------
    # Never fatal: local voice conversation does not depend on it.
    try:
        from remote_control.service import start_bridge
        if start_bridge() is None:
            print("[JARVIS] NEXUS bridge unavailable — remote control is off.")
    except Exception as e:
        print(f"[JARVIS] NEXUS bridge failed to start: {e}")

    def _signal_handler(sig, frame):
        print(f"\n[Shutdown] Signal {sig} received, shutting down...")
        ui._win._quit_app()

    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)

    def runner():
        try:
            asyncio.run(JarvisLive(ui).run())
        except KeyboardInterrupt:
            print("\n[Shutdown] KeyboardInterrupt received.")
        except SystemExit:
            print("\n[Shutdown] SystemExit received.")
        except Exception as e:
            print(f"\n[Shutdown] Error: {e}")
            traceback.print_exc()
        finally:
            print("[Shutdown] Runner thread exiting.")

    threading.Thread(target=runner, daemon=True, name="JarvisCore").start()
    server.set_state("READY")
    print("[JARVIS] Starting command deck...")
    print("[JARVIS] Say 'Jarvis' or press Ctrl+Space to activate")
    try:
        sys.exit(app.exec())
    finally:
        process_registry.unregister()


if __name__ == "__main__":
    main()
