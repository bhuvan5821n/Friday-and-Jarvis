"""Phase 6 tests: remote screenshots.

Mostly about what must not happen: no secure-desktop capture, no image on disk
afterwards, no pixels in the log, no camera, no continuous mode.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from remote_control import screenshot
from remote_control.screenshot import ScreenshotError, capture, describe


class TestSecureDesktop(unittest.TestCase):

    def test_nothing_is_captured_while_the_secure_desktop_is_up(self):
        with mock.patch.object(screenshot, "secure_desktop_active",
                               return_value=True):
            with self.assertRaises(ScreenshotError) as ctx:
                capture()
        self.assertIn("secure screen", str(ctx.exception))

    def test_an_unknown_desktop_state_is_treated_as_secure(self):
        """If we cannot prove the screen is safe, we do not capture it."""
        with mock.patch.dict(sys.modules, {"win32service": None,
                                           "win32con": None}):
            self.assertTrue(screenshot.secure_desktop_active())

    def test_the_check_runs_before_any_capture_call(self):
        grab = mock.Mock()
        with mock.patch.object(screenshot, "secure_desktop_active",
                               return_value=True), \
                mock.patch.dict(sys.modules,
                                {"PIL": mock.Mock(), "PIL.ImageGrab": grab}):
            with self.assertRaises(ScreenshotError):
                capture()
        self.assertFalse(grab.grab.called)


class TestNoTraceLeftBehind(unittest.TestCase):

    def setUp(self):
        self.image = mock.Mock(size=(1920, 1080))
        self.image.resize.return_value = self.image
        pil = mock.Mock()
        pil.ImageGrab.grab.return_value = self.image
        pil.Image.LANCZOS = 1
        self.patches = [
            mock.patch.object(screenshot, "secure_desktop_active",
                              return_value=False),
            mock.patch.dict(sys.modules, {"PIL": pil}),
        ]
        for patch in self.patches:
            patch.start()
            self.addCleanup(patch.stop)
        self.pil = pil

    def _run(self):
        saved = {}

        def fake_save(path, *a, **k):
            saved["path"] = path
            Path(path).write_bytes(b"\x89PNG" + b"x" * 100)

        self.image.save.side_effect = fake_save
        data, meta = capture()
        return data, meta, saved["path"]

    def test_the_temp_file_is_deleted_after_delivery(self):
        _, _, path = self._run()
        self.assertFalse(Path(path).exists(), "the screenshot was left on disk")

    def test_the_temp_file_is_deleted_even_when_reading_fails(self):
        self.image.save.side_effect = RuntimeError("save exploded")
        with self.assertRaises(ScreenshotError):
            capture()
        leftovers = list(Path(__import__("tempfile").gettempdir())
                         .glob("nexus_shot_*.png"))
        self.assertEqual(leftovers, [])

    def test_only_metadata_is_returned_not_a_path(self):
        _, meta, _ = self._run()
        self.assertEqual(set(meta), {"width", "height", "bytes", "ms"})
        self.assertNotIn("path", meta)

    def test_pixels_are_never_logged(self):
        with self.assertLogs("nexus.screenshot", level="DEBUG") as logs:
            data, _, _ = self._run()
        joined = "\n".join(logs.output)
        self.assertNotIn("PNG", joined)
        self.assertNotIn(str(data[:8]), joined)

    def test_the_metadata_describes_what_was_sent_not_what_was_on_screen(self):
        """A 1920-wide screen delivered at 1280 must not report 1920."""
        resized = mock.Mock(size=(screenshot.MAX_WIDTH, 720))
        self.image.resize.return_value = resized
        resized.save.side_effect = lambda path, *a, **k: Path(path).write_bytes(
            b"\x89PNG")
        _, meta = capture()
        self.assertEqual(meta["width"], screenshot.MAX_WIDTH)
        self.assertEqual(meta["height"], 720)

    def test_a_large_screen_is_downscaled_for_the_phone(self):
        self._run()
        self.assertTrue(self.image.resize.called)
        self.assertEqual(self.image.resize.call_args.args[0][0],
                         screenshot.MAX_WIDTH)


class TestNoSurveillance(unittest.TestCase):

    def _code_lines(self):
        """Code only. The module's prose names these hazards to rule them out;
        only an actual reference in code is a defect."""
        source = Path(screenshot.__file__).read_text(encoding="utf-8")
        body = source.split('"""', 2)[-1]
        return [line for line in body.splitlines()
                if line.strip() and not line.lstrip().startswith("#")]

    def test_there_is_no_camera_or_microphone_access(self):
        for line in self._code_lines():
            for forbidden in ("camera", "cv2", "VideoCapture", "microphone",
                              "pyaudio", "sounddevice"):
                self.assertNotIn(forbidden, line, line)

    def test_there_is_no_continuous_capture_loop(self):
        for line in self._code_lines():
            for forbidden in ("while True", "schedule", "interval", "Thread("):
                self.assertNotIn(forbidden, line, line)

    def test_capture_takes_no_count_or_interval_argument(self):
        import inspect
        self.assertEqual(list(inspect.signature(capture).parameters), [])


class TestDescription(unittest.TestCase):

    def test_the_summary_is_metadata_only(self):
        text = describe({"width": 1280, "height": 720, "bytes": 40960})
        self.assertIn("1280x720", text)
        self.assertIn("40 KB", text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
