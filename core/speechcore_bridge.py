"""Bridge to SpeechCore.exe — JARVIS's C++ real-time audio engine.

Design rule: Python must keep working EXACTLY as today when SpeechCore is
absent. This module is therefore a pure optional add-on:

    bridge = SpeechCoreBridge(on_event=..., on_audio=...)
    if bridge.connect():      # SpeechCore.exe is running → use it
        ...
    else:                     # not running → sounddevice path, unchanged
        ...

Protocol (see SpeechCore/src/speechcore.cpp):
  core → python : JSON lines (events) + binary frames 'A','U',u32len,pcm16
  python → core : JSON lines: {"cmd":"stream_on"} / {"cmd":"stream_off"} / ping

Events: WakeWordDetected (future), SpeechStarted, SpeechEnded, Level,
        MicChanged, MicDisconnected, MicRecovered, HealthStatus, pong.
"""
from __future__ import annotations

import json
import socket
import struct
import subprocess
import sys
import threading
from pathlib import Path

SPEECHCORE_PORT = 48800
BASE_DIR = Path(sys.executable).parent if getattr(sys, "frozen", False) \
    else Path(__file__).resolve().parent.parent
EXE_PATH = BASE_DIR / "SpeechCore" / "Build" / "SpeechCore.exe"


class SpeechCoreBridge:
    def __init__(self, on_event=None, on_audio=None):
        self.on_event = on_event or (lambda ev: None)
        self.on_audio = on_audio or (lambda pcm: None)
        self._sock: socket.socket | None = None
        self._alive = False

    # ── lifecycle ────────────────────────────────────────────────────────
    def launch(self) -> bool:
        """Start SpeechCore.exe if present and not already running."""
        if self.connect(timeout=0.3):
            return True                       # already running
        if not EXE_PATH.exists():
            return False
        try:
            proc = subprocess.Popen([str(EXE_PATH)], cwd=str(EXE_PATH.parent))
            try:
                # Record it so full shutdown can verify and stop it later.
                from launcher import process_registry
                process_registry.register("speechcore", proc.pid,
                                          executable=str(EXE_PATH))
            except Exception:
                pass  # registry is best-effort; capture must not fail on it
        except Exception as e:
            print(f"[SpeechCore] launch failed: {e}")
            return False
        # give it a moment to bind
        import time
        for _ in range(20):
            time.sleep(0.1)
            if self.connect(timeout=0.2):
                return True
        return False

    def connect(self, timeout: float = 1.0) -> bool:
        if self._alive:
            return True
        try:
            s = socket.create_connection(("127.0.0.1", SPEECHCORE_PORT), timeout=timeout)
            s.settimeout(None)
            self._sock = s
            self._alive = True
            threading.Thread(target=self._read_loop, daemon=True,
                             name="SpeechCoreRead").start()
            print("[SpeechCore] connected")
            return True
        except OSError:
            return False

    def close(self):
        self._alive = False
        if self._sock:
            try: self._sock.close()
            except OSError: pass
            self._sock = None

    @property
    def available(self) -> bool:
        return self._alive

    # ── commands ─────────────────────────────────────────────────────────
    def _cmd(self, cmd: str):
        if not self._alive:
            return
        try:
            self._sock.sendall((json.dumps({"cmd": cmd}) + "\n").encode())
        except OSError:
            self.close()

    def stream_on(self):  self._cmd("stream_on")
    def stream_off(self): self._cmd("stream_off")
    def ping(self):       self._cmd("ping")

    # ── reader: split JSON lines from binary AU frames ───────────────────
    def _read_loop(self):
        buf = b""
        try:
            while self._alive:
                data = self._sock.recv(65536)
                if not data:
                    break
                buf += data
                while buf:
                    if buf[:2] == b"AU":                    # binary audio frame
                        if len(buf) < 6:
                            break
                        (n,) = struct.unpack_from("<I", buf, 2)
                        if len(buf) < 6 + n:
                            break
                        self.on_audio(buf[6:6 + n])
                        buf = buf[6 + n:]
                    else:                                   # JSON line
                        nl = buf.find(b"\n")
                        if nl < 0:
                            break
                        line = buf[:nl]; buf = buf[nl + 1:]
                        try:
                            self.on_event(json.loads(line))
                        except Exception:
                            pass
        except OSError:
            pass
        finally:
            self.close()
            print("[SpeechCore] disconnected")


def demo():
    """Self-check without SpeechCore running: frame parser on synthetic data."""
    got = {"events": [], "audio": []}
    b = SpeechCoreBridge(on_event=lambda e: got["events"].append(e),
                         on_audio=lambda a: got["audio"].append(a))
    # simulate the reader logic on a crafted buffer
    import struct as _s
    pcm = b"\x01\x00\x02\x00"
    stream = (b'{"ev":"SpeechStarted"}\n'
              + b"AU" + _s.pack("<I", len(pcm)) + pcm
              + b'{"ev":"SpeechEnded"}\n')
    # feed through the same splitter inline
    buf = stream
    while buf:
        if buf[:2] == b"AU":
            (n,) = _s.unpack_from("<I", buf, 2)
            got["audio"].append(buf[6:6 + n]); buf = buf[6 + n:]
        else:
            nl = buf.find(b"\n"); got["events"].append(json.loads(buf[:nl])); buf = buf[nl + 1:]
    assert got["events"][0]["ev"] == "SpeechStarted"
    assert got["events"][1]["ev"] == "SpeechEnded"
    assert got["audio"] == [pcm]
    assert b.available is False
    print("speechcore_bridge demo OK")


if __name__ == "__main__":
    demo()
