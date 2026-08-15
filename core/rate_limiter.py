"""Simple rate limiter for JARVIS tools.

Prevents accidental or malicious rapid execution of destructive commands.
"""
import time
from threading import Lock
from collections import defaultdict


class RateLimiter:
    """Per-tool rate limiter with cooldown periods."""

    def __init__(self):
        self._last_call: dict[str, float] = defaultdict(float)
        self._call_count: dict[str, int] = defaultdict(int)
        self._lock = Lock()
        
        # Default cooldowns in seconds for different tool categories
        self._cooldowns = {
            # Destructive operations - longer cooldowns
            "shutdown_jarvis": 5.0,
            "computer_settings": 0.5,  # Volume, brightness, etc.
            "file_controller": 0.3,
            "desktop_control": 0.5,
            "browser_control": 0.2,
            "computer_control": 0.1,
            "send_message": 2.0,  # Prevent spam
            "instagram": 2.0,
            "reminder": 1.0,
        }
        
        # Burst limits (max calls within window)
        self._burst_limits = {
            "send_message": (5, 60),      # 5 messages per 60 seconds
            "instagram": (3, 60),          # 3 actions per 60 seconds
            "shutdown_jarvis": (2, 30),    # 2 shutdown attempts per 30 seconds
            "file_controller": (10, 30),   # 10 file ops per 30 seconds
        }
        self._burst_windows: dict[str, list[float]] = defaultdict(list)

    def allow(self, tool_name: str) -> tuple[bool, str]:
        """Check if tool call is allowed. Returns (allowed, reason)."""
        with self._lock:
            now = time.time()
            
            # Check cooldown
            cooldown = self._cooldowns.get(tool_name, 0.1)
            elapsed = now - self._last_call[tool_name]
            if elapsed < cooldown:
                remaining = cooldown - elapsed
                return False, f"Rate limited: wait {remaining:.1f}s before calling {tool_name} again"
            
            # Check burst limit
            if tool_name in self._burst_limits:
                max_calls, window = self._burst_limits[tool_name]
                window_start = now - window
                # Remove old entries
                self._burst_windows[tool_name] = [
                    t for t in self._burst_windows[tool_name] if t > window_start
                ]
                if len(self._burst_windows[tool_name]) >= max_calls:
                    oldest = self._burst_windows[tool_name][0]
                    wait_time = oldest + window - now
                    return False, f"Burst limit: {tool_name} limited to {max_calls} calls per {window}s. Wait {wait_time:.1f}s"
                self._burst_windows[tool_name].append(now)
            
            self._last_call[tool_name] = now
            return True, ""

    def set_cooldown(self, tool_name: str, seconds: float):
        """Set custom cooldown for a tool."""
        with self._lock:
            self._cooldowns[tool_name] = seconds


# Global instance
limiter = RateLimiter()


def check_rate_limit(tool_name: str) -> str | None:
    """Check rate limit. Returns error message if blocked, None if allowed."""
    allowed, reason = limiter.allow(tool_name)
    return None if allowed else reason
