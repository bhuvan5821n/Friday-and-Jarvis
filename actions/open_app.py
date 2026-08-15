import time
import subprocess
import platform
import shutil

_SYSTEM = platform.system()

_APP_ALIASES: dict[str, dict[str, str]] = {

    "chrome":             {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "google chrome":      {"Windows": "chrome",                  "Darwin": "Google Chrome",        "Linux": "google-chrome"},
    "firefox":            {"Windows": "firefox",                 "Darwin": "Firefox",              "Linux": "firefox"},
    "edge":               {"Windows": "msedge",                  "Darwin": "Microsoft Edge",       "Linux": "microsoft-edge"},
    "brave":              {"Windows": "brave",                   "Darwin": "Brave Browser",        "Linux": "brave-browser"},
    "safari":             {"Windows": "msedge",                  "Darwin": "Safari",               "Linux": "firefox"},
    "opera":              {"Windows": "opera",                   "Darwin": "Opera",                "Linux": "opera"},
    "whatsapp":           {"Windows": "WhatsApp",                "Darwin": "WhatsApp",             "Linux": "whatsapp"},
    "telegram":           {"Windows": "Telegram",                "Darwin": "Telegram",             "Linux": "telegram"},
    "discord":            {"Windows": "Discord",                 "Darwin": "Discord",              "Linux": "discord"},
    "slack":              {"Windows": "Slack",                   "Darwin": "Slack",                "Linux": "slack"},
    "zoom":               {"Windows": "Zoom",                    "Darwin": "zoom.us",              "Linux": "zoom"},
    "teams":              {"Windows": "msteams",                 "Darwin": "Microsoft Teams",      "Linux": "teams"},
    "skype":              {"Windows": "skype",                   "Darwin": "Skype",                "Linux": "skype"},
    "signal":             {"Windows": "signal",                  "Darwin": "Signal",               "Linux": "signal"},
    "spotify":            {"Windows": "Spotify",                 "Darwin": "Spotify",              "Linux": "spotify"},
    "vlc":                {"Windows": "vlc",                     "Darwin": "VLC",                  "Linux": "vlc"},
    "netflix":            {"Windows": "Netflix",                 "Darwin": "Netflix",              "Linux": "firefox"},
    "vscode":             {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "visual studio code": {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "code":               {"Windows": "code",                    "Darwin": "Visual Studio Code",   "Linux": "code"},
    "terminal":           {"Windows": "wt",                      "Darwin": "Terminal",             "Linux": "gnome-terminal"},
    "cmd":                {"Windows": "cmd.exe",                 "Darwin": "Terminal",             "Linux": "bash"},
    "powershell":         {"Windows": "powershell.exe",          "Darwin": "Terminal",             "Linux": "bash"},
    "postman":            {"Windows": "Postman",                 "Darwin": "Postman",              "Linux": "postman"},
    "git":                {"Windows": "git-bash",                "Darwin": "Terminal",             "Linux": "bash"},
    "figma":              {"Windows": "Figma",                   "Darwin": "Figma",                "Linux": "figma"},
    "blender":            {"Windows": "blender",                 "Darwin": "Blender",              "Linux": "blender"},
    "word":               {"Windows": "winword",                 "Darwin": "Microsoft Word",       "Linux": "libreoffice --writer"},
    "excel":              {"Windows": "excel",                   "Darwin": "Microsoft Excel",      "Linux": "libreoffice --calc"},
    "powerpoint":         {"Windows": "powerpnt",                "Darwin": "Microsoft PowerPoint", "Linux": "libreoffice --impress"},
    "libreoffice":        {"Windows": "soffice",                 "Darwin": "LibreOffice",          "Linux": "libreoffice"},
    "notepad":            {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "textedit":           {"Windows": "notepad.exe",             "Darwin": "TextEdit",             "Linux": "gedit"},
    "explorer":           {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "file explorer":      {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "finder":             {"Windows": "explorer.exe",            "Darwin": "Finder",               "Linux": "nautilus"},
    "task manager":       {"Windows": "taskmgr.exe",             "Darwin": "Activity Monitor",     "Linux": "gnome-system-monitor"},
    "settings":           {"Windows": "ms-settings:",            "Darwin": "System Preferences",   "Linux": "gnome-control-center"},
    "calculator":         {"Windows": "calc.exe",                "Darwin": "Calculator",           "Linux": "gnome-calculator"},
    "paint":              {"Windows": "mspaint.exe",             "Darwin": "Preview",              "Linux": "gimp"},
    "instagram":          {"Windows": "Instagram",               "Darwin": "Instagram",            "Linux": "firefox"},
    "tiktok":             {"Windows": "TikTok",                  "Darwin": "TikTok",               "Linux": "firefox"},
    "notion":             {"Windows": "Notion",                  "Darwin": "Notion",               "Linux": "notion"},
    "obsidian":           {"Windows": "Obsidian",                "Darwin": "Obsidian",             "Linux": "obsidian"},
    "capcut":             {"Windows": "CapCut",                  "Darwin": "CapCut",               "Linux": "capcut"},
    "steam":              {"Windows": "steam",                   "Darwin": "Steam",                "Linux": "steam"},
    "epic":               {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
    "epic games":         {"Windows": "EpicGamesLauncher",       "Darwin": "Epic Games Launcher",  "Linux": "legendary"},
}


def _normalize(raw: str) -> str:
    """Exact alias match only.

    Substring matching used to live here, so "epic fail" resolved to the Epic
    Games Launcher and "code review" launched VS Code. A near-miss now falls
    through to the raw name, where the launcher fails honestly instead of
    starting the wrong application.
    """
    key = raw.lower().strip()
    if key in _APP_ALIASES:
        return _APP_ALIASES[key].get(_SYSTEM, raw)
    return raw


def _launch_windows(app_name: str) -> tuple[bool, str]:
    """Return (started, method). Says nothing about whether the app is up."""
    resolved = shutil.which(app_name) or shutil.which(app_name.split(".")[0])
    if resolved:
        try:
            # No shell: the resolved absolute path goes straight to CreateProcess,
            # so an app name containing `&` or `|` cannot become a second command.
            subprocess.Popen(
                [resolved],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return True, "path"
        except Exception as e:
            print(f"[open_app] direct launch failed: {e}")

    # Shell handlers (`ms-settings:`, `mailto:`) have no executable to resolve.
    # `explorer.exe` takes the URI as one argument, so still no shell.
    if ":" in app_name and " " not in app_name:
        try:
            subprocess.Popen(["explorer.exe", app_name],
                             stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True, "uri"
        except Exception:
            pass

    # Store apps are not on PATH, so the Start Menu is the only way to reach
    # them. Kept because it genuinely works — but it used to `return True`
    # unconditionally, which is where "Opened Chrome." came from when nothing
    # had opened. It now reports only that keystrokes were sent; the verifier
    # decides whether anything started.
    try:
        import pyautogui
        pyautogui.PAUSE = 0.1
        pyautogui.press("win")
        time.sleep(0.7)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.9)
        pyautogui.press("enter")
        return True, "start_menu"
    except Exception as e:
        print(f"[open_app] Start Menu search failed: {e}")

    return False, "not_found"


def _launch_macos(app_name: str) -> tuple[bool, str]:

    for candidate in (app_name, f"{app_name}.app"):
        try:
            result = subprocess.run(["open", "-a", candidate],
                                    capture_output=True, timeout=8)
            if result.returncode == 0:
                return True, "open"
        except Exception:
            pass

    binary = shutil.which(app_name) or shutil.which(app_name.lower())
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True, "path"
        except Exception:
            pass

    try:
        import pyautogui
        pyautogui.hotkey("command", "space")
        time.sleep(0.6)
        pyautogui.write(app_name, interval=0.05)
        time.sleep(0.8)
        pyautogui.press("enter")
        return True, "spotlight"
    except Exception as e:
        print(f"[open_app] Spotlight failed: {e}")

    return False, "not_found"


def _launch_linux(app_name: str) -> tuple[bool, str]:

    binary = (
        shutil.which(app_name) or
        shutil.which(app_name.lower()) or
        shutil.which(app_name.lower().replace(" ", "-")) or
        shutil.which(app_name.lower().replace(" ", "_"))
    )
    if binary:
        try:
            subprocess.Popen([binary], stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL)
            return True, "path"
        except Exception:
            pass

    for desktop_name in [
        app_name.lower(),
        app_name.lower().replace(" ", "-"),
        app_name.lower().replace(" ", ""),
    ]:
        try:
            result = subprocess.run(["gtk-launch", desktop_name],
                                    capture_output=True, timeout=5)
            if result.returncode == 0:
                return True, "gtk_launch"
        except Exception:
            pass

    # `xdg-open` last: it returns 0 for almost anything, including names that
    # open nothing, so it was previously the source of confident false success.
    try:
        result = subprocess.run(["xdg-open", app_name],
                                capture_output=True, timeout=5)
        if result.returncode == 0:
            return True, "xdg_open"
    except Exception:
        pass

    return False, "not_found"


_OS_LAUNCHERS = {
    "Windows": _launch_windows,
    "Darwin":  _launch_macos,
    "Linux":   _launch_linux,
}

def open_app(
    parameters=None,
    response=None,
    player=None,
    session_memory=None,
) -> str:
    """Launch, then check. The returned sentence reports what was observed.

    Only the first of these claims success:
      "Opened X."                            — a new X process is running
      "X was already open."                  — it was running before we tried
      "I started X but couldn't confirm it." — launched, no evidence either way
      "I tried, but X isn't running."        — launched, nothing appeared
    """
    from aoca import verify as verification

    app_name = (parameters or {}).get("app_name", "").strip()

    if not app_name:
        return "No application name provided."

    launcher = _OS_LAUNCHERS.get(_SYSTEM)
    if launcher is None:
        return f"Unsupported operating system: {_SYSTEM}"

    normalized = _normalize(app_name)
    # ASCII arrow: this line goes to a cp1252 console on Windows, where "→"
    # raises UnicodeEncodeError and takes the whole launch down with it.
    print(f"[open_app] Launching: '{app_name}' -> '{normalized}' ({_SYSTEM})")

    if player:
        player.write_log(f"[open_app] {app_name}")

    stem = normalized.rsplit(".", 1)[0].lower()
    # Taken before launching: without a before-set, an instance that was already
    # running is indistinguishable from one this call started.
    before = verification.snapshot_processes(stem)
    started_at = time.monotonic()

    try:
        started, method = launcher(normalized)
        if not started and normalized.lower() != app_name.lower():
            started, method = launcher(app_name)
    except Exception as e:
        print(f"[open_app] Error: {e}")
        return f"Failed to open {app_name}: {e}"

    execution = verification.ExecutionResult(
        execution_started=started,
        tool="open_app",
        method=method,
        duration_ms=int((time.monotonic() - started_at) * 1000),
        error=None if started else f"I couldn't find {app_name} on this machine.",
    )

    result = verification.verify("application_open", {
        "before": before,
        "expected_name": normalized,
    })

    if result.evidence.get("already_running"):
        return f"{app_name} was already open."

    outcome = verification.combine("open_app", execution, result,
                                   subject=app_name)

    from aoca.events import EventState, emit
    from aoca.outcomes import store
    emit("action.outcome",
         EventState.COMPLETED if outcome.succeeded else EventState.FAILED,
         **outcome.as_event())
    store.record(outcome)

    return outcome.message