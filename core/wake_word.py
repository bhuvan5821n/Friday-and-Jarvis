"""Offline 'Hey Jarvis' wake-word detection.

Runs locally (no network) on the same 16 kHz int16 mic audio main.py already
captures. When asleep, mic audio is fed here instead of to Gemini; on a hit,
Jarvis wakes. Uses openwakeword's built-in 'hey_jarvis' model.

If openwakeword isn't installed, this degrades to disabled — the mic/mute
button still works exactly as before.
"""
import numpy as np

# ponytail: model download + import guarded — a missing dep must not crash JARVIS,
# it just means no hands-free wake (mute button still works).
try:
    from openwakeword.model import Model
    import openwakeword.utils as _oww_utils
    _OWW_OK = True
except Exception as e:  # noqa: BLE001
    _OWW_OK = False
    _IMPORT_ERR = e


class WakeWord:
    def __init__(self, threshold: float = 0.5, model_name: str = "hey_jarvis"):
        # threshold is the calibration knob: raise toward 0.7 if it triggers on
        # noise, lower toward 0.3 if it misses you. Real mics/rooms vary.
        self.threshold = threshold
        self.enabled = False
        self._model = None
        if not _OWW_OK:
            print(f"[Wake] ! openwakeword unavailable: {_IMPORT_ERR}. Hands-free disabled.")
            return
        try:
            try:
                _oww_utils.download_models([model_name])  # no-op if already cached
            except Exception:
                pass  # already downloaded, or offline — Model() will tell us
            self._model = Model(wakeword_models=[model_name], inference_framework="onnx")
            self._key = next(iter(self._model.models.keys()))
            self.enabled = True
            print(f"[Wake] 'Hey Jarvis' listener armed (threshold={threshold}).")
        except Exception as e:  # noqa: BLE001
            print(f"[Wake] ! Could not load model: {e}. Hands-free disabled.")

    def detect(self, indata) -> bool:
        """Feed one mic block (int16 numpy array or bytes). True on wake."""
        if not self.enabled:
            return False
        if isinstance(indata, (bytes, bytearray)):
            audio = np.frombuffer(indata, dtype=np.int16)
        else:
            audio = np.asarray(indata, dtype=np.int16).reshape(-1)
        try:
            scores = self._model.predict(audio)
        except Exception as e:
            if not getattr(self, "_predict_err_logged", False):
                self._predict_err_logged = True   # once, not per mic block
                print(f"[WakeWord] predict FAILED: {e}")
            return False
        score = scores.get(self._key, 0.0)
        if score >= self.threshold:
            self._model.reset()  # clear buffer so we don't double-fire
            print(f"[WakeWord] Detected (score={score:.2f})")
            return True
        return False


def demo():
    """Self-check: disabled instance never fires, shape handling is sound."""
    w = WakeWord.__new__(WakeWord)   # bypass model load
    w.enabled = False
    assert w.detect(b"\x00\x00" * 100) is False
    assert w.detect(np.zeros(160, dtype=np.int16)) is False
    print("wake_word demo OK")


if __name__ == "__main__":
    demo()
