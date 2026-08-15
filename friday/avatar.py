"""FRIDAY's face.

The face is video, never geometry.  Nothing in this module draws eyes, a mouth,
a head outline, or any facial feature: the MP4 clips in ``assets/friday/avatar``
are the only source of FRIDAY's likeness.  What *is* drawn here is the
holographic surround — rings, halo and glow — which reacts to real microphone
and TTS amplitude supplied by the backend.

Frames, not video widgets
-------------------------
Each deck pulls frames from a ``QVideoSink`` and this widget paints them
itself.  ``QVideoWidget`` was rejected because it renders into a native surface
that ignores ``QGraphicsOpacityEffect`` — the crossfade would silently become a
hard cut, and the result cannot be screenshotted for visual review.  Painting
the frames directly gives exact control over the blend, guarantees the square
aspect, and lets the surround composite correctly around the face.

Two players, one visible
------------------------
Switching a single player's source produces a black frame while the new file is
demuxed.  Instead an active/standby pair is kept: the next clip is loaded into
the hidden player and the crossfade only begins once that player has delivered
a real frame.  The outgoing player is released after the fade, so at most two
decoders are ever alive regardless of how many emotions exist.

Audio
-----
Every clip ships a silent AAC track.  No ``QAudioOutput`` is ever attached and
the audio track is deselected once demuxed, so the avatar can never contend
with real TTS speech for the output device.
"""
from __future__ import annotations

import logging
import math

from PyQt6.QtCore import (QEasingCurve, QObject, QPointF, QRectF, Qt, QTimer,
                          QUrl, pyqtSignal, pyqtSlot)
from PyQt6.QtGui import QColor, QImage, QPainter, QPen, QRadialGradient
from PyQt6.QtMultimedia import QMediaPlayer, QVideoFrame, QVideoSink
from PyQt6.QtWidgets import QSizePolicy, QWidget

from .assets import AvatarLibrary, EmotionClip, library

log = logging.getLogger("friday.avatar")

#: Crossfade length. Long enough to hide a decoder hand-off, short enough that
#: an emotion change still reads as a reaction rather than a dissolve.
TRANSITION_MS = 200

#: A loop is restarted this long before the clip ends so the standby deck is
#: already presenting frames when the seam arrives.
LOOP_LEAD_MS = 400

#: Guard against a clip that never delivers a frame (corrupt or codec-less):
#: after this long we swap anyway rather than freezing on the old emotion.
LOAD_TIMEOUT_MS = 1500


class _Deck(QObject):
    """One decoder: a player, its sink, and the most recent frame it produced."""

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.sink = QVideoSink(self)
        self.player = QMediaPlayer(self)
        self.player.setVideoSink(self.sink)
        # No QAudioOutput is ever attached, so the silent AAC track has nowhere
        # to go. Deselecting it as well is belt-and-braces, and must happen
        # after demuxing — hence the tracksChanged hook.
        self.player.tracksChanged.connect(self._mute_embedded_audio)
        self.player.setLoops(1)
        self.sink.videoFrameChanged.connect(self._on_frame)

        self.clip: EmotionClip | None = None
        self.image: QImage | None = None
        self.opacity = 0.0

    def _on_frame(self, frame: QVideoFrame) -> None:
        if not frame.isValid():
            return
        image = frame.toImage()
        if not image.isNull():
            self.image = image

    def _mute_embedded_audio(self) -> None:
        try:
            if self.player.audioTracks():
                self.player.setActiveAudioTrack(-1)
        except Exception:  # Backend without track-selection support.
            pass

    @property
    def has_frame(self) -> bool:
        return self.image is not None

    def load(self, clip: EmotionClip) -> None:
        self.clip = clip
        self.image = None
        self.player.setSource(QUrl.fromLocalFile(str(clip.path.resolve())))

    def release(self) -> None:
        self.player.stop()
        self.player.setSource(QUrl())
        self.clip = None
        self.image = None
        self.opacity = 0.0


class FridayAvatar(QWidget):
    """FRIDAY's canonical avatar: one component, every emotion.

    The widget keeps the face in a centred square box, so FRIDAY's 1:1 geometry
    survives any window size.  Callers drive it through :meth:`set_emotion` and
    the amplitude setters; it invents no motion of its own beyond a slow idle
    rotation of the surround.
    """

    emotion_changed = pyqtSignal(str)
    media_error = pyqtSignal(str)

    def __init__(self, parent: QWidget | None = None,
                 lib: AvatarLibrary | None = None):
        super().__init__(parent)
        self._lib = lib or library()

        self._emotion = "neutral"
        self._pending: str | None = None
        self._previous_emotion = "neutral"
        self._reduced_motion = False

        self._mic_level = 0.0
        self._tts_level = 0.0
        self._speaking = False
        self._connected = True
        self._accent = QColor(0, 212, 255)

        self._phase = 0.0
        self._fade = 0.0
        self._fading = False

        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.setMinimumSize(220, 220)
        self.setAttribute(Qt.WidgetAttribute.WA_OpaquePaintEvent, False)

        self._active = _Deck(self)
        self._standby = _Deck(self)
        self._active.opacity = 1.0
        for deck in (self._active, self._standby):
            deck.player.errorOccurred.connect(self._on_media_error)

        # One timer drives the surround, the crossfade and the loop seam, so the
        # widget never accumulates a timer per effect.
        self._ticker = QTimer(self)
        self._ticker.setInterval(33)          # ~30fps, matched to the clips
        self._ticker.timeout.connect(self._tick)

        self._load_guard = QTimer(self)
        self._load_guard.setSingleShot(True)
        self._load_guard.timeout.connect(self._force_swap)

        self._start_emotion("neutral")
        self._ticker.start()

    # ---- public API ----------------------------------------------------

    @property
    def emotion(self) -> str:
        return self._emotion

    @property
    def previous_emotion(self) -> str:
        return self._previous_emotion

    @property
    def current_clip(self) -> EmotionClip | None:
        return self._active.clip

    @pyqtSlot(str)
    def set_emotion(self, name: str) -> None:
        """Crossfade to ``name``; falls back to neutral when unavailable."""
        if name == self._emotion and not self._fading and self._pending is None:
            return
        clip = self._lib.get(name)
        if clip is None:
            log.error("FRIDAY has no playable clip for %r and no fallback", name)
            self.media_error.emit(f"no clip for '{name}'")
            return
        if self._fading:
            # Settle the in-flight transition and start the newest request from
            # a clean state rather than queueing dissolves on top of each other.
            self._complete_swap()
        self._previous_emotion = self._emotion
        self._pending = clip.name
        self._standby.load(clip)
        self._standby.player.play()
        self._load_guard.start(LOAD_TIMEOUT_MS)

    def set_microphone_level(self, level: float) -> None:
        self._mic_level = max(0.0, min(1.0, float(level)))

    def set_tts_level(self, level: float) -> None:
        self._tts_level = max(0.0, min(1.0, float(level)))

    def set_speaking(self, speaking: bool) -> None:
        self._speaking = bool(speaking)

    def set_connected(self, connected: bool) -> None:
        self._connected = bool(connected)
        self.update()

    def set_accent(self, color: QColor | str) -> None:
        self._accent = QColor(color)
        self.update()

    def set_reduced_motion(self, reduced: bool) -> None:
        """Freeze the surround animation; the clip itself keeps playing."""
        self._reduced_motion = bool(reduced)

    def set_paused(self, paused: bool) -> None:
        """Suspend decoding and effects while FRIDAY is hidden or minimized."""
        if paused:
            self._ticker.stop()
            self._active.player.pause()
            self._standby.player.pause()
        else:
            if not self._ticker.isActive():
                self._ticker.start()
            self._active.player.play()

    def playback_position(self) -> int:
        return self._active.player.position()

    def playback_duration(self) -> int:
        return self._active.player.duration()

    def media_status(self) -> str:
        return self._active.player.mediaStatus().name

    def _on_media_error(self, error: QMediaPlayer.Error, message: str = "") -> None:
        """Surface decoder failures instead of leaving a blank avatar."""
        if error == QMediaPlayer.Error.NoError:
            return
        detail = message or error.name
        log.error("FRIDAY avatar media error: %s", detail)
        self.media_error.emit(detail)

    # ---- transition machinery ------------------------------------------

    def _start_emotion(self, name: str) -> None:
        """Load an emotion into the active deck with no crossfade (startup)."""
        clip = self._lib.get(name)
        if clip is None:
            log.error("FRIDAY cannot start: no clip for %r", name)
            self.media_error.emit(f"no clip for '{name}'")
            return
        self._active.load(clip)
        self._active.player.play()
        self._active.opacity = 1.0
        self._emotion = clip.name
        self.emotion_changed.emit(self._emotion)

    def _force_swap(self) -> None:
        """The standby never produced a frame; swap anyway so FRIDAY is not stuck."""
        if self._pending is not None and not self._fading:
            log.warning("FRIDAY clip %r produced no frame within %dms; swapping anyway",
                        self._pending, LOAD_TIMEOUT_MS)
            self._fading = True
            self._fade = 0.0

    def _complete_swap(self) -> None:
        self._active, self._standby = self._standby, self._active
        self._active.opacity = 1.0
        self._standby.release()
        self._fading = False
        self._fade = 0.0
        if self._active.clip is not None:
            self._emotion = self._active.clip.name
            self.emotion_changed.emit(self._emotion)
        self._pending = None
        self._load_guard.stop()

    def _check_loop_seam(self) -> None:
        """Restart looping clips slightly early to avoid a black seam frame.

        The clips have no guarantee that their first and last frames match, so
        the same file is crossfaded into itself rather than hard-cut.
        """
        if self._fading or self._pending is not None:
            return
        clip = self._active.clip
        if clip is None or not clip.loop:
            return
        duration = self._active.player.duration()
        if duration <= 0:
            return
        if duration - self._active.player.position() <= LOOP_LEAD_MS:
            self._pending = clip.name
            self._standby.load(clip)
            self._standby.player.play()
            self._load_guard.start(LOAD_TIMEOUT_MS)

    def _tick(self) -> None:
        if not self._reduced_motion:
            self._phase += 0.012

        # Begin the fade only once the incoming deck actually has a picture;
        # this is what keeps a black frame off the screen.
        if self._pending is not None and not self._fading and self._standby.has_frame:
            self._fading = True
            self._fade = 0.0
            self._load_guard.stop()

        if self._fading:
            self._fade = min(1.0, self._fade + (33.0 / TRANSITION_MS))
            eased = QEasingCurve(QEasingCurve.Type.InOutQuad).valueForProgress(self._fade)
            self._standby.opacity = eased
            self._active.opacity = 1.0 - eased
            if self._fade >= 1.0:
                self._complete_swap()
        else:
            self._check_loop_seam()

        self.update()

    # ---- painting ------------------------------------------------------

    def _face_rect(self) -> QRectF:
        """The centred square the face is drawn into — always 1:1."""
        side = min(self.width(), self.height())
        return QRectF((self.width() - side) / 2.0, (self.height() - side) / 2.0,
                      float(side), float(side))

    def paintEvent(self, _event):
        """Paint the video frames, then the surround. No facial geometry."""
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)

        face = self._face_rect()
        centre = QPointF(face.center())
        radius = face.width() / 2.0

        # Amplitude comes from real audio; with nothing speaking or heard the
        # surround sits still rather than pretending to react.
        amplitude = self._tts_level if self._speaking else self._mic_level

        # --- glow behind the face
        halo = QRadialGradient(centre, radius * 1.28)
        glow = QColor(self._accent)
        glow.setAlpha(int(24 + 56 * amplitude))
        halo.setColorAt(0.50, QColor(0, 0, 0, 0))
        halo.setColorAt(0.80, glow)
        halo.setColorAt(1.0, QColor(0, 0, 0, 0))
        p.setPen(Qt.PenStyle.NoPen)
        p.setBrush(halo)
        p.drawEllipse(centre, radius * 1.28, radius * 1.28)

        # --- the face itself: source frames, blended, never reshaped
        for deck in (self._active, self._standby):
            if deck.image is None or deck.opacity <= 0.001:
                continue
            p.setOpacity(min(1.0, deck.opacity))
            p.drawImage(face, deck.image, QRectF(deck.image.rect()))
        p.setOpacity(1.0)

        # --- holographic surround
        ring = QColor(255, 176, 32) if not self._connected else QColor(self._accent)
        for i, (scale, width, span) in enumerate(
                ((1.03, 1.6, 190), (1.10, 1.1, 120), (1.17, 0.9, 70))):
            r = radius * scale + radius * 0.025 * amplitude
            ring.setAlpha(int(80 + 110 * amplitude) if i == 0
                          else int(40 + 70 * amplitude))
            p.setPen(QPen(ring, width))
            p.setBrush(Qt.BrushStyle.NoBrush)
            direction = 1 if i % 2 == 0 else -1
            start = int((math.degrees(self._phase) * direction * (0.6 + i * 0.4)) % 360)
            box = QRectF(centre.x() - r, centre.y() - r, r * 2, r * 2)
            p.drawArc(box, start * 16, span * 16)
        p.end()
