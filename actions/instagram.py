"""Instagram assistant — DOM/accessibility-driven, never coordinates.

Rides on browser_control's persistent Playwright Chrome session (real user
profile), so the Instagram login survives restarts. Element search uses
roles, aria-labels, placeholders and visible text with fallbacks — layout
changes degrade gracefully to a spoken error, never a crash.

Safety: draft and send are separate actions. A message is TYPED into the
box by `draft` and only leaves when the user explicitly confirms → `send`.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

try:
    from actions.browser_control import _registry
except ImportError:          # direct `python actions/instagram.py` run
    from browser_control import _registry

IG = "https://www.instagram.com"

# ── Chrome profile pinning: always the profile owning this Google account ──

def _config() -> dict:
    base = Path(sys.executable).parent if getattr(sys, "frozen", False) \
        else Path(__file__).resolve().parent.parent
    try:
        return json.loads((base / "config" / "api_keys.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def _target_email() -> str:
    email = _config().get("chrome_profile_email", "")
    if not email:
        raise ValueError(
            "Chrome profile email not configured. "
            "Set 'chrome_profile_email' in config/api_keys.json "
            "or configure it in FRIDAY settings."
        )
    return email.lower()


def _profile_for_email(email: str) -> str | None:
    """Chrome profile directory name ('Default', 'Profile 2', …) owning `email`."""
    root = Path(os.environ.get("LOCALAPPDATA", "")) / "Google" / "Chrome" / "User Data"
    try:   # primary source: Local State metadata
        state = json.loads((root / "Local State").read_text(encoding="utf-8"))
        for name, info in state.get("profile", {}).get("info_cache", {}).items():
            if str(info.get("user_name", "")).lower() == email:
                return name
    except Exception:
        pass
    # fallback: each profile's Preferences → account_info
    for prefs_path in root.glob("*/Preferences"):
        try:
            prefs = json.loads(prefs_path.read_text(encoding="utf-8"))
            for acc in prefs.get("account_info", []):
                if str(acc.get("email", "")).lower() == email:
                    return prefs_path.parent.name
        except Exception:
            continue
    return None

# last draft, so `send` can confirm who it went to
_pending: dict = {"username": None, "message": None}


# ── page helpers (run inside the browser session's event loop) ──────────

async def _dismiss(page):
    """Close 'Turn on Notifications' and similar popups if present."""
    for label in ("Not Now", "Not now", "Cancel"):
        try:
            btn = page.get_by_role("button", name=label).first
            if await btn.count() and await btn.is_visible():
                await btn.click(timeout=2_000)
        except Exception:
            pass


async def _logged_in(page) -> bool:
    if "accounts/login" in page.url:
        return False
    try:
        if await page.locator('input[name="username"]').count():
            return False
    except Exception:
        pass
    for sel in ('svg[aria-label="Home"]',
                'a[href="/direct/inbox/"]',
                'svg[aria-label="Direct"]',
                'svg[aria-label="New post"]'):
        try:
            if await page.locator(sel).count():
                return True
        except Exception:
            pass
    return False


async def _find(page, builders, timeout=8_000):
    """First visible element among several locator strategies, or None."""
    for build in builders:
        try:
            loc = build().first
            await loc.wait_for(state="visible", timeout=timeout)
            return loc
        except Exception:
            continue
    return None


async def _message_box(page):
    return await _find(page, [
        lambda: page.locator('div[role="textbox"][contenteditable="true"]'),
        lambda: page.locator('div[aria-label="Message" i][contenteditable="true"]'),
        lambda: page.get_by_placeholder(re.compile("message", re.I)),
    ])


# ── actions ─────────────────────────────────────────────────────────────

async def _open(sess) -> str:
    page = await sess._get_page()
    if "instagram.com" not in page.url:
        await page.goto(IG + "/", wait_until="domcontentloaded", timeout=45_000)
    try:
        await page.wait_for_load_state("networkidle", timeout=12_000)
    except Exception:
        pass
    await _dismiss(page)
    if not await _logged_in(page):
        return ("Instagram is open but the session is logged out. "
                "Tell the user to log in once in the opened window — "
                "the session will be remembered after that.")
    # identity check: never message from the wrong account
    expected = _config().get("instagram_username", "").strip().lower()
    if expected:
        try:
            if not await page.locator(f'a[href="/{expected}/"]').count():
                return (f"WRONG ACCOUNT: Instagram is not logged in as {expected}. "
                        "Stop — do not send anything. Tell the user to switch accounts.")
        except Exception:
            pass
    return "Instagram is open and logged in."


async def _draft(sess, username: str, message: str) -> str:
    status = await _open(sess)
    if "logged out" in status or "WRONG ACCOUNT" in status:
        return status
    page = await sess._get_page()

    # New-message dialog: deterministic entry point, no hunting for buttons
    await page.goto(IG + "/direct/new/", wait_until="domcontentloaded", timeout=45_000)
    await _dismiss(page)

    box = await _find(page, [
        lambda: page.locator('input[name="queryBox"]'),
        lambda: page.get_by_placeholder(re.compile("search", re.I)),
        lambda: page.locator('div[role="dialog"] input'),
    ])
    if box is None:
        return "Could not find the recipient search box — Instagram may have changed its layout."

    await box.fill(username)
    await page.wait_for_timeout(2_000)   # let suggestions load

    # exact username first, then partial match
    hit = await _find(page, [
        lambda: page.locator('div[role="dialog"]').get_by_text(username, exact=True),
        lambda: page.locator('div[role="dialog"]').get_by_text(username, exact=False),
    ], timeout=6_000)
    if hit is None:
        return f"User not found: {username}."
    await hit.click()

    btn = await _find(page, [
        lambda: page.get_by_role("button", name=re.compile(r"^(chat|next)$", re.I)),
        lambda: page.get_by_role("dialog").get_by_role("button", name=re.compile("chat|next", re.I)),
    ], timeout=6_000)
    if btn is None:
        return f"Selected {username} but could not find the Chat button."
    await btn.click()

    msg_box = await _message_box(page)
    if msg_box is None:
        return f"Chat with {username} opened, but the message box was not found."

    _pending.update(username=username, message=message or None)
    if not message:
        return f"Chat with {username} is open."

    await msg_box.click()
    await msg_box.type(message, delay=30)
    return (f'DRAFT READY — NOT SENT. To {username}: "{message}". '
            "Read this draft to the user and wait for explicit confirmation "
            "before calling send.")


async def _send(sess) -> str:
    page = await sess._get_page()
    msg_box = await _message_box(page)
    if msg_box is None:
        return "No open chat with a message box — draft a message first."
    try:
        text = (await msg_box.inner_text()).strip()
    except Exception:
        text = ""
    if not text:
        return "The message box is empty — nothing to send. Draft a message first."

    await msg_box.press("Enter")
    await page.wait_for_timeout(1_000)
    try:
        still = (await msg_box.inner_text()).strip()
    except Exception:
        still = ""
    who = _pending.get("username") or "the recipient"
    _pending.update(username=None, message=None)
    if still:
        return f"Pressed send but the text is still in the box — sending to {who} may have failed."
    return f"Message sent to {who}."


# ── dispatcher (sync, called from the tool executor) ────────────────────

_ACTIONS = ("open", "draft", "message", "chat", "send")


def instagram(parameters: dict = None, response=None, player=None,
              session_memory=None) -> str:
    p        = parameters or {}
    action   = str(p.get("action", "open")).lower().strip()
    username = str(p.get("username", "")).strip()
    message  = str(p.get("message", "")).strip()

    if action not in _ACTIONS:
        return f"Unknown instagram action: {action}. Use: {', '.join(_ACTIONS)}."
    if action in ("draft", "message", "chat") and not username:
        return "Which username should I open? Ask the user."

    email   = _target_email()
    profile = _profile_for_email(email)
    if profile is None:
        return (f"Could not find any Chrome profile logged into {email}. "
                "Ask the user which Chrome profile to use.")

    try:
        sess = _registry.get("chrome", profile_directory=profile)
    except Exception as e:
        return f"Could not start Chrome: {e}"

    try:
        if action == "open":
            result = sess.run(_open(sess), timeout=90)
        elif action == "send":
            result = sess.run(_send(sess), timeout=60)
        else:   # draft / message / chat
            result = sess.run(_draft(sess, username, message), timeout=120)
    except Exception as e:
        result = f"Instagram error ({action}): {e}"

    short = str(result)[:100]
    print(f"[Instagram] {short}")
    if player:
        player.write_log(f"[instagram] {short[:60]}")
    return result


def demo():
    """Self-check without launching a browser or touching the network."""
    assert "Unknown" in instagram({"action": "fly_to_moon"})
    assert "username" in instagram({"action": "draft"}).lower()
    assert _pending == {"username": None, "message": None}
    assert "open" in _ACTIONS and "send" in _ACTIONS
    email = _target_email()
    prof  = _profile_for_email(email)
    print(f"target email: {email} -> profile: {prof}")
    assert _profile_for_email("nobody@nowhere.invalid") is None
    print("instagram demo OK")


if __name__ == "__main__":
    demo()
