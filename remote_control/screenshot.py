"""Phase 6: remote screenshots.

A screenshot is the most invasive thing this gateway can do, so the rules are
narrow and enforced here rather than trusted to a caller:

  * one shot per request — there is no loop, no interval, no continuous mode
  * nothing is captured while the secure desktop is up (lock screen, UAC,
    Windows password entry): those pixels are exactly what must never leave
  * the temp file is deleted after delivery, in a finally block
  * only metadata is logged — dimensions and bytes, never the image
  * there is no camera and no microphone here, and a test asserts it

Capture uses the same library the desktop app already depends on, so this adds
no new dependency and no second screen pipeline.
"""
from __future__ import annotations

import logging
import os
import tempfile
import time

log = logging.getLogger("nexus.screenshot")

#: Delivered images are downscaled to this width. A phone cannot use more, and
#: a smaller file spends less time on disk.
MAX_WIDTH = 1280

SECURE_DESKTOP_REFUSAL = (
    "The laptop is showing a secure screen (lock screen, UAC prompt, or "
    "password entry). I do not capture those.")


class ScreenshotError(RuntimeError):
    """Capture failed. The message is safe to send back."""


def secure_desktop_active() -> bool:
    """True when Windows is showing the lock screen or a UAC/secure prompt.

    Checked by asking which desktop the input is attached to: on the secure
    desktop the name is "Winlogon", and an ordinary process cannot open it at
    all — so a failure to open is itself a positive signal, not an error.
    """
    try:
        import win32con
        import win32service
    except Exception as exc:
        # Without the check we cannot prove the screen is safe to capture, and
        # an unprovable secure desktop must be treated as present.
        log.warning("cannot determine desktop state: %s", exc)
        return True

    desktop = None
    try:
        desktop = win32service.OpenInputDesktop(0, False, win32con.MAXIMUM_ALLOWED)
        name = win32service.GetUserObjectInformation(
            desktop, win32con.UOI_NAME)
        return str(name).lower() != "default"
    except Exception:
        # OpenInputDesktop fails precisely when the secure desktop owns input.
        return True
    finally:
        if desktop is not None:
            try:
                desktop.CloseDesktop()
            except Exception:
                pass


def capture() -> tuple[bytes, dict]:
    """Take one screenshot. Returns (png_bytes, metadata).

    The file exists on disk only for as long as it takes to read it back, and
    is removed in a finally block so a delivery failure cannot leave an image
    behind.
    """
    if secure_desktop_active():
        raise ScreenshotError(SECURE_DESKTOP_REFUSAL)

    try:
        from PIL import Image, ImageGrab
    except Exception as exc:
        raise ScreenshotError("Screen capture is not available on this build.") \
            from exc

    started = time.monotonic()
    handle, path = tempfile.mkstemp(prefix="nexus_shot_", suffix=".png")
    os.close(handle)
    try:
        image = ImageGrab.grab()
        if image.size[0] > MAX_WIDTH:
            ratio = MAX_WIDTH / float(image.size[0])
            image = image.resize((MAX_WIDTH, int(image.size[1] * ratio)),
                                 Image.LANCZOS)
        # Read the size back off the delivered image, so the metadata describes
        # what was actually sent rather than what was on screen.
        width, height = image.size
        image.save(path, "PNG", optimize=True)
        with open(path, "rb") as handle:
            data = handle.read()
    except ScreenshotError:
        raise
    except Exception as exc:
        log.warning("capture failed: %s", exc)
        raise ScreenshotError("The screenshot could not be taken.") from exc
    finally:
        try:
            os.remove(path)
        except OSError:
            log.warning("could not remove the temporary screenshot")

    meta = {"width": width, "height": height, "bytes": len(data),
            "ms": int((time.monotonic() - started) * 1000)}
    # Metadata only. The image itself is never logged, at any level.
    log.info("screenshot %dx%d, %d bytes, %dms", meta["width"], meta["height"],
             meta["bytes"], meta["ms"])
    return data, meta


def describe(meta: dict) -> str:
    return (f"Screen: {meta.get('width')}x{meta.get('height')}, "
            f"{round(meta.get('bytes', 0) / 1024)} KB.")
