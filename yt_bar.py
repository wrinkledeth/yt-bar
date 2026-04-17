import ctypes
import hashlib
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
import traceback
from collections import deque
from ctypes import byref, c_uint32, c_void_p
from dataclasses import dataclass, field

import AppKit
import AVFoundation
import Foundation
import numpy as np
import objc
import rumps


INTERNAL_SAMPLE_RATE = 48000
CHANNELS = 2
PCM_BUFFER_FRAMES = 4096
PCM_BYTES_PER_FRAME = CHANNELS * 4
SCHEDULE_AHEAD_SECONDS = 0.5
SCHEDULE_AHEAD_FRAMES = int(INTERNAL_SAMPLE_RATE * SCHEDULE_AHEAD_SECONDS)
WORKER_TICK_SECONDS = 0.05
ROUTE_CHANGE_DEBOUNCE_SECONDS = 0.25
ROUTE_RETRY_DELAYS = (0.35, 1.0)
YTDLP_FIELD_SEP = "\x1f"
PROGRESS_BAR_WIDTH = 22
VISUALIZER_SNAPSHOT_FRAMES = 256
VISUALIZER_TAP_BUFFER_FRAMES = 1024
DECODER_QUEUE_BUFFERS = 24
CACHE_DELAY_SECONDS = 10.0
CACHE_WORKER_COUNT = 2
RECENT_MENU_LIMIT = 10
RECENT_TITLE_LIMIT = 55
SEEK_TRACE_LOGGING = True
PARTIAL_CACHE_SUFFIX = ".partial.opus"
APP_ROOT = os.path.dirname(os.path.abspath(__file__))
SONGS_DIR_NAME = "songs"
SONGS_DIR = os.path.join(APP_ROOT, SONGS_DIR_NAME)
RECENT_INDEX_PATH = os.path.join(SONGS_DIR, "recent.json")
MEDIA_PLAYER_FRAMEWORK_PATH = "/System/Library/Frameworks/MediaPlayer.framework"
REMOTE_SKIP_INTERVAL_SECONDS = 30.0
MP_REMOTE_COMMAND_STATUS_SUCCESS = 0
MP_REMOTE_COMMAND_STATUS_NO_SUCH_CONTENT = 100
MP_REMOTE_COMMAND_STATUS_COMMAND_FAILED = 200

# Stereometer grid: 3 braille chars wide (6 cols) x 4 rows = 6x4 dot grid
GRID_W = 6  # dot columns (3 braille chars x 2 cols each)
GRID_H = 4  # dot rows per braille char

BRAILLE_BASE = 0x2800
DOT_BITS = [
    [0x40, 0x04, 0x02, 0x01],  # col 0 (left): bottom to top
    [0x80, 0x20, 0x10, 0x08],  # col 1 (right): bottom to top
]
SAFE_CACHE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _fourcc(value):
    return int.from_bytes(value.encode("ascii"), "big")


class AudioObjectPropertyAddress(ctypes.Structure):
    _fields_ = [
        ("mSelector", c_uint32),
        ("mScope", c_uint32),
        ("mElement", c_uint32),
    ]


AUDIO_OBJECT_SYSTEM_OBJECT = 1
AUDIO_OBJECT_UNKNOWN = 0
AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL = _fourcc("glob")
AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN = 0
AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE = _fourcc("dOut")


try:
    _CORE_AUDIO = ctypes.CDLL(
        "/System/Library/Frameworks/CoreAudio.framework/CoreAudio"
    )
    _AUDIO_OBJECT_GET_PROPERTY_DATA = _CORE_AUDIO.AudioObjectGetPropertyData
    _AUDIO_OBJECT_GET_PROPERTY_DATA.argtypes = [
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        c_uint32,
        c_void_p,
        ctypes.POINTER(c_uint32),
        c_void_p,
    ]
    _AUDIO_OBJECT_GET_PROPERTY_DATA.restype = ctypes.c_int32

    _AUDIO_OBJECT_LISTENER_PROC = ctypes.CFUNCTYPE(
        ctypes.c_int32,
        c_uint32,
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        c_void_p,
    )

    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER = _CORE_AUDIO.AudioObjectAddPropertyListener
    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER.argtypes = [
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        _AUDIO_OBJECT_LISTENER_PROC,
        c_void_p,
    ]
    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER.restype = ctypes.c_int32

    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER = (
        _CORE_AUDIO.AudioObjectRemovePropertyListener
    )
    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER.argtypes = [
        c_uint32,
        ctypes.POINTER(AudioObjectPropertyAddress),
        _AUDIO_OBJECT_LISTENER_PROC,
        c_void_p,
    ]
    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER.restype = ctypes.c_int32
except OSError:
    _AUDIO_OBJECT_GET_PROPERTY_DATA = None
    _AUDIO_OBJECT_LISTENER_PROC = None
    _AUDIO_OBJECT_ADD_PROPERTY_LISTENER = None
    _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER = None


def get_default_output_device_id():
    if _AUDIO_OBJECT_GET_PROPERTY_DATA is None:
        return None

    address = AudioObjectPropertyAddress(
        AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE,
        AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
        AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
    )
    device_id = c_uint32(AUDIO_OBJECT_UNKNOWN)
    size = c_uint32(ctypes.sizeof(device_id))
    status = _AUDIO_OBJECT_GET_PROPERTY_DATA(
        AUDIO_OBJECT_SYSTEM_OBJECT,
        byref(address),
        0,
        None,
        byref(size),
        byref(device_id),
    )
    if status != 0 or device_id.value == AUDIO_OBJECT_UNKNOWN:
        return None
    return int(device_id.value)


def log_exception(context, exc):
    print(f"{context}: {exc!r}")
    print(f"{context} args: {getattr(exc, 'args', ())!r}")
    traceback.print_exc()


@dataclass
class PlayRequest:
    url: str
    duration: float
    is_local: bool = False
    start_time: float = 0.0
    paused: bool = False
    on_finished: object = None
    on_stopped: object = None
    retry_kind: str | None = None
    retry_attempt: int = 0


@dataclass
class PlaybackSession:
    id: int
    request: PlayRequest
    stop_event: threading.Event = field(default_factory=threading.Event)
    decoder_stop_event: threading.Event | None = None
    decoded_queue: queue.Queue = field(
        default_factory=lambda: queue.Queue(maxsize=DECODER_QUEUE_BUFFERS)
    )
    engine: object | None = None
    player: object | None = None
    mixer: object | None = None
    format: object | None = None
    notification_observer: object | None = None
    tap_block: object | None = None
    decoder_thread: threading.Thread | None = None
    ytdlp_process: subprocess.Popen | None = None
    ffmpeg_process: subprocess.Popen | None = None
    decoder_generation: int = 0
    decoder_eof: bool = False
    decoder_failed: bool = False
    decoder_error: str | None = None
    scheduled_buffers: dict = field(default_factory=dict)
    scheduled_frames_total: int = 0
    next_buffer_id: int = 0
    started_playback: bool = False
    last_rendered_frames: int = 0
    last_elapsed_seconds: float = 0.0
    rebuild_pending: bool = False
    rebuild_deadline: float = 0.0
    completion_count: int = 0
    seek_trace_id: int = 0
    seek_trace_started_at: float = 0.0
    seek_trace_target: float = 0.0
    seek_trace_first_chunk_logged: bool = False
    seek_trace_first_buffer_logged: bool = False
    seek_trace_player_play_logged: bool = False
    seek_trace_elapsed_logged: bool = False

    @property
    def duration(self):
        return self.request.duration

    @property
    def url(self):
        return self.request.url

    @property
    def is_local(self):
        return self.request.is_local

    @property
    def paused(self):
        return self.request.paused

    @paused.setter
    def paused(self, value):
        self.request.paused = value

    @property
    def base_offset_seconds(self):
        return self.request.start_time

    @base_offset_seconds.setter
    def base_offset_seconds(self, value):
        self.request.start_time = value

    @property
    def on_finished(self):
        return self.request.on_finished

    @property
    def on_stopped(self):
        return self.request.on_stopped


class EngineConfigurationObserver(Foundation.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(EngineConfigurationObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def handleConfigChange_(self, notification):
        callback = getattr(self, "_callback", None)
        if callback:
            callback()


class RecentMenuObserver(Foundation.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(RecentMenuObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def menuWillOpen_(self, menu):
        callback = getattr(self, "_callback", None)
        if callback:
            callback()


class RemoteCommandBridge(Foundation.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(RemoteCommandBridge, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    @objc.python_method
    def _dispatch(self, method_name):
        owner = getattr(self, "_owner", None)
        if owner is None:
            return 0
        handler = getattr(owner, method_name, None)
        if handler is None:
            return 0
        try:
            return int(handler())
        except Exception as exc:
            log_exception(f"Remote command failed: {method_name}", exc)
            return int(owner._remote_command_status_command_failed())

    @objc.typedSelector(b"q@:@")
    def handlePlayCommand_(self, event):
        return self._dispatch("_handle_remote_play_command")

    @objc.typedSelector(b"q@:@")
    def handlePauseCommand_(self, event):
        return self._dispatch("_handle_remote_pause_command")

    @objc.typedSelector(b"q@:@")
    def handleTogglePlayPauseCommand_(self, event):
        return self._dispatch("_handle_remote_toggle_command")

    @objc.typedSelector(b"q@:@")
    def handleSkipForwardCommand_(self, event):
        return self._dispatch("_handle_remote_skip_forward_command")

    @objc.typedSelector(b"q@:@")
    def handleSkipBackwardCommand_(self, event):
        return self._dispatch("_handle_remote_skip_backward_command")

    @objc.typedSelector(b"q@:@")
    def handleNextTrackCommand_(self, event):
        return self._dispatch("_handle_remote_skip_forward_command")

    @objc.typedSelector(b"q@:@")
    def handlePreviousTrackCommand_(self, event):
        return self._dispatch("_handle_remote_skip_backward_command")


class CommonModeTimerTarget(Foundation.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(CommonModeTimerTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    @objc.typedSelector(b"v@:@")
    def fire_(self, timer):
        try:
            self._callback(timer)
        except Exception as exc:
            log_exception("common-mode timer", exc)


def _schedule_common_mode_timer(interval, callback):
    target = CommonModeTimerTarget.alloc().initWithCallback_(callback)
    timer = Foundation.NSTimer.alloc().initWithFireDate_interval_target_selector_userInfo_repeats_(
        Foundation.NSDate.date(),
        interval,
        target,
        b"fire:",
        None,
        True,
    )
    Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(
        timer, Foundation.NSRunLoopCommonModes
    )
    return timer, target


def stable_hash(value):
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def sanitize_cache_key(value):
    cleaned = SAFE_CACHE_KEY_RE.sub("_", value or "").strip("._")
    return cleaned or stable_hash(value or "track")


def default_source_url(info, fallback_url=""):
    for key in ("webpage_url", "original_url", "url"):
        value = info.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value

    video_id = info.get("id")
    extractor = (
        info.get("extractor_key")
        or info.get("ie_key")
        or info.get("extractor")
        or ""
    )
    lower_fallback = fallback_url.lower()
    if (
        isinstance(video_id, str)
        and video_id
        and (
            "youtube" in str(extractor).lower()
            or "youtube.com" in lower_fallback
            or "youtu.be" in lower_fallback
        )
    ):
        return f"https://www.youtube.com/watch?v={video_id}"

    return fallback_url


def cache_relpath_for_id(item_id):
    return os.path.join(SONGS_DIR_NAME, f"{sanitize_cache_key(item_id)}.opus")


def absolute_repo_path(path):
    if os.path.isabs(path):
        return path
    return os.path.join(APP_ROOT, path)


def partial_cache_abspath_for_id(item_id):
    return os.path.join(SONGS_DIR, f"{sanitize_cache_key(item_id)}{PARTIAL_CACHE_SUFFIX}")


def truncate_title(title, limit=RECENT_TITLE_LIMIT):
    text = (title or "").strip() or "Unknown"
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return f"{text[: limit - 1]}…"


@dataclass
class TrackInfo:
    id: str
    title: str
    duration: float
    source_url: str
    local_path: str

    @property
    def absolute_local_path(self):
        return absolute_repo_path(self.local_path)

    @property
    def partial_local_path(self):
        return partial_cache_abspath_for_id(self.id)

    def is_cached(self):
        return os.path.exists(self.absolute_local_path)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "duration": self.duration,
            "source_url": self.source_url,
            "local_path": self.local_path,
        }

    @classmethod
    def from_dict(cls, data):
        track_id = sanitize_cache_key(str(data.get("id") or stable_hash(data.get("source_url", ""))))
        return cls(
            id=track_id,
            title=(data.get("title") or "Unknown").strip() or "Unknown",
            duration=parse_duration(data.get("duration")),
            source_url=(data.get("source_url") or "").strip(),
            local_path=(
                data.get("local_path")
                or cache_relpath_for_id(track_id)
            ),
        )


@dataclass
class ResolvedItem:
    kind: str
    id: str
    title: str
    source_url: str
    tracks: list[TrackInfo]

    @property
    def cache_key(self):
        return f"{self.kind}:{self.id}"

    def is_fully_cached(self):
        return bool(self.tracks) and all(track.is_cached() for track in self.tracks)

    def cached_tracks(self):
        return [track for track in self.tracks if track.is_cached()]


@dataclass
class RecentItem:
    kind: str
    id: str
    title: str
    source_url: str
    last_played: float
    tracks: list[TrackInfo]

    @property
    def cache_key(self):
        return f"{self.kind}:{self.id}"

    def to_dict(self):
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "source_url": self.source_url,
            "last_played": self.last_played,
            "tracks": [track.to_dict() for track in self.tracks],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            kind=(data.get("kind") or "video").strip() or "video",
            id=sanitize_cache_key(str(data.get("id") or stable_hash(data.get("source_url", "")))),
            title=(data.get("title") or "Unknown").strip() or "Unknown",
            source_url=(data.get("source_url") or "").strip(),
            last_played=float(data.get("last_played") or 0.0),
            tracks=[
                TrackInfo.from_dict(track_data)
                for track_data in data.get("tracks", [])
                if isinstance(track_data, dict)
            ],
        )


@dataclass
class CacheJob:
    item: ResolvedItem
    track: TrackInfo


@dataclass(frozen=True)
class MediaPlayerSupport:
    command_center_class: object
    now_playing_info_center_class: object
    command_status_success: int
    command_status_command_failed: int
    command_status_no_such_content: int
    property_elapsed_playback_time: str
    property_playback_rate: str
    property_title: str
    property_playback_duration: str


class AudioEngine:
    def __init__(self):
        self._lock = threading.RLock()
        self._commands = queue.Queue()
        self._notification_center = Foundation.NSNotificationCenter.defaultCenter()
        self._session_counter = 0
        self._current_session_id = 0
        self._active = False
        self._starting = False
        self._paused = False
        self._current_is_local = False
        self._duration = 0.0
        self._elapsed_seconds = 0.0
        self._dot_grid = np.zeros((GRID_W, GRID_H), dtype=np.float32)
        self._dot_decay = 0.75
        self._rms_peak = 0.001
        self._viz_snapshot = None
        self._output_listener_callback = None
        self._output_listener_registered = False
        self._output_listener_address = AudioObjectPropertyAddress(
            AUDIO_HARDWARE_PROPERTY_DEFAULT_OUTPUT_DEVICE,
            AUDIO_OBJECT_PROPERTY_SCOPE_GLOBAL,
            AUDIO_OBJECT_PROPERTY_ELEMENT_MAIN,
        )
        self._seek_trace_counter = 0
        self._pending_retry_request = None
        self._pending_retry_at = 0.0
        self._worker = threading.Thread(target=self._worker_loop, daemon=True)
        self._worker.start()
        self._install_default_output_listener()

    @property
    def is_playing(self):
        with self._lock:
            return self._active and not self._starting and not self._paused

    @property
    def is_active(self):
        with self._lock:
            return self._active or self._starting

    @property
    def elapsed(self):
        with self._lock:
            if self._active or self._starting:
                return self._elapsed_seconds
            return 0.0

    @property
    def duration(self):
        with self._lock:
            return self._duration

    @property
    def is_paused(self):
        with self._lock:
            return (self._active or self._starting) and self._paused

    @property
    def dot_grid(self):
        with self._lock:
            if self._viz_snapshot is not None:
                self._compute_stereometer(self._viz_snapshot)
                self._viz_snapshot = None
            return self._dot_grid.copy()

    def play(
        self,
        url,
        on_finished=None,
        on_stopped=None,
        duration=0,
        is_local=False,
        start_time=0,
        paused=False,
    ):
        request = PlayRequest(
            url=url,
            duration=duration,
            is_local=is_local,
            start_time=max(0.0, float(start_time)),
            paused=paused,
            on_finished=on_finished,
            on_stopped=on_stopped,
        )
        self._publish_state(
            session_id=0,
            active=True,
            starting=not paused,
            paused=paused,
            is_local=is_local,
            duration=duration,
            elapsed=request.start_time,
            reset_grid=True,
        )
        self._enqueue_command("play", request)

    def toggle_pause(self):
        with self._lock:
            if not (self._active or self._starting):
                return False
            target = not self._paused
            self._paused = target
        self._enqueue_command("set_paused", target)
        return target

    def seek_current(self, start_time):
        with self._lock:
            if (
                not (self._active or self._starting)
                or not self._current_is_local
                or self._current_session_id == 0
            ):
                return False
            paused = self._paused
            duration = self._duration

        target = max(0.0, float(start_time))
        self._publish_state(
            active=True,
            starting=not paused,
            paused=paused,
            duration=duration,
            elapsed=target,
            reset_grid=True,
            is_local=True,
        )
        self._enqueue_command("seek_current", target)
        return True

    def stop(self):
        self._enqueue_command("stop", "user")
        self._publish_stopped()

    def close(self):
        self._remove_default_output_listener()
        self._enqueue_command("shutdown")
        self._worker.join(timeout=2)
        self._publish_stopped()

    def _enqueue_command(self, name, *payload):
        self._commands.put((name, *payload))

    def _publish_state(
        self,
        *,
        session_id=None,
        active=None,
        starting=None,
        paused=None,
        is_local=None,
        duration=None,
        elapsed=None,
        reset_grid=False,
    ):
        with self._lock:
            if session_id is not None:
                self._current_session_id = session_id
            if active is not None:
                self._active = active
            if starting is not None:
                self._starting = starting
            if paused is not None:
                self._paused = paused
            if is_local is not None:
                self._current_is_local = bool(is_local)
            if duration is not None:
                self._duration = duration
            if elapsed is not None:
                self._elapsed_seconds = max(0.0, float(elapsed))
            if reset_grid:
                self._dot_grid = np.zeros((GRID_W, GRID_H), dtype=np.float32)
                self._rms_peak = 0.001
                self._viz_snapshot = None

    def _publish_stopped(self):
        self._publish_state(
            session_id=0,
            active=False,
            starting=False,
            paused=False,
            is_local=False,
            duration=0.0,
            elapsed=0.0,
            reset_grid=True,
        )

    def _worker_loop(self):
        current_session = None

        while True:
            timeout = WORKER_TICK_SECONDS
            if current_session is None and self._pending_retry_request is not None:
                timeout = max(
                    0.0,
                    min(timeout, self._pending_retry_at - time.monotonic()),
                )

            commands = []
            try:
                commands.append(self._commands.get(timeout=timeout))
                while True:
                    commands.append(self._commands.get_nowait())
            except queue.Empty:
                pass

            for command in commands:
                current_session, should_stop = self._handle_command(
                    current_session,
                    command,
                )
                if should_stop:
                    if current_session is not None:
                        self._discard_session(current_session, reason="shutdown")
                    self._pending_retry_request = None
                    self._publish_stopped()
                    return

            if current_session is None and self._pending_retry_request is not None:
                if time.monotonic() >= self._pending_retry_at:
                    request = self._pending_retry_request
                    self._pending_retry_request = None
                    current_session = self._attempt_start_request(request)

            if current_session is not None:
                current_session = self._service_session(current_session)

    def _handle_command(self, current_session, command):
        name = command[0]

        if name == "play":
            request = command[1]
            self._pending_retry_request = None
            if current_session is not None:
                self._discard_session(current_session, reason="replaced")
            return self._attempt_start_request(request), False

        if name == "stop":
            self._pending_retry_request = None
            if current_session is not None:
                self._discard_session(current_session, reason=command[1])
            self._publish_stopped()
            return None, False

        if name == "set_paused":
            target = command[1]
            if current_session is not None:
                self._set_paused(current_session, target)
            return current_session, False

        if name == "seek_current":
            target = command[1]
            if current_session is None or not current_session.is_local:
                return current_session, False

            self._begin_seek_trace(current_session, target)
            request = self._resume_request(current_session, start_time=target)
            if current_session.rebuild_pending:
                self._log_seek_trace(
                    current_session,
                    "fallback_full_restart",
                    reason="rebuild_pending",
                )
                self._discard_session(
                    current_session,
                    reason="seek_during_rebuild",
                    clear_public_state=False,
                    notify_stopped=False,
                )
                return self._attempt_start_request(request), False

            try:
                self._restart_local_decoder(current_session, target)
                return current_session, False
            except Exception as exc:
                self._log_seek_trace(
                    current_session,
                    "fallback_full_restart",
                    reason="fast_seek_failed",
                    error=str(exc),
                )
                log_exception("Fast local seek failed", exc)
                self._discard_session(
                    current_session,
                    reason="fast_seek_fallback",
                    clear_public_state=False,
                    notify_stopped=False,
                )
                return self._attempt_start_request(request), False

        if name == "route_event":
            reason = command[1]
            session_id = command[2]
            if current_session is None or session_id not in (None, current_session.id):
                return current_session, False
            current_session.rebuild_pending = True
            current_session.rebuild_deadline = (
                time.monotonic() + ROUTE_CHANGE_DEBOUNCE_SECONDS
            )
            print(
                "Route change detected",
                {"reason": reason, "session_id": current_session.id},
            )
            return current_session, False

        if name == "decoder_eof":
            session_id, generation = command[1], command[2]
            if (
                current_session is not None
                and current_session.id == session_id
                and current_session.decoder_generation == generation
            ):
                current_session.decoder_eof = True
            return current_session, False

        if name == "decoder_failed":
            session_id, generation, error_text = command[1], command[2], command[3]
            if (
                current_session is not None
                and current_session.id == session_id
                and current_session.decoder_generation == generation
            ):
                current_session.decoder_failed = True
                current_session.decoder_error = error_text
            return current_session, False

        if name == "buffer_complete":
            session_id, buffer_id, _callback_type = command[1], command[2], command[3]
            if current_session is not None and current_session.id == session_id:
                current_session.scheduled_buffers.pop(buffer_id, None)
                current_session.completion_count += 1
            return current_session, False

        if name == "shutdown":
            self._pending_retry_request = None
            return current_session, True

        return current_session, False

    def _attempt_start_request(self, request):
        try:
            return self._create_session(request)
        except Exception as exc:
            log_exception("AudioEngine start failure", exc)
            if request.retry_kind == "route_change":
                if request.retry_attempt < len(ROUTE_RETRY_DELAYS):
                    next_request = PlayRequest(
                        url=request.url,
                        duration=request.duration,
                        is_local=request.is_local,
                        start_time=request.start_time,
                        paused=request.paused,
                        on_finished=request.on_finished,
                        on_stopped=request.on_stopped,
                        retry_kind=request.retry_kind,
                        retry_attempt=request.retry_attempt + 1,
                    )
                    delay = ROUTE_RETRY_DELAYS[request.retry_attempt]
                    self._pending_retry_request = next_request
                    self._pending_retry_at = time.monotonic() + delay
                    self._publish_state(
                        session_id=0,
                        active=True,
                        starting=not request.paused,
                        paused=request.paused,
                        is_local=request.is_local,
                        duration=request.duration,
                        elapsed=request.start_time,
                    )
                    return None

            self._publish_stopped()
            if request.on_stopped:
                request.on_stopped()
            return None

    def _create_session(self, request):
        self._session_counter += 1
        session = PlaybackSession(id=self._session_counter, request=request)
        session.last_elapsed_seconds = request.start_time

        try:
            self._build_engine(session)
            self._start_decoder_thread(session)
        except Exception:
            self._discard_session(
                session,
                reason="startup_failure",
                clear_public_state=False,
                notify_stopped=False,
            )
            raise

        self._publish_state(
            session_id=session.id,
            active=True,
            starting=not request.paused,
            paused=request.paused,
            is_local=request.is_local,
            duration=request.duration,
            elapsed=request.start_time,
            reset_grid=True,
        )
        return session

    def _begin_seek_trace(self, session, target):
        if not SEEK_TRACE_LOGGING:
            return

        self._seek_trace_counter += 1
        session.seek_trace_id = self._seek_trace_counter
        session.seek_trace_started_at = time.perf_counter()
        session.seek_trace_target = max(0.0, float(target))
        session.seek_trace_first_chunk_logged = False
        session.seek_trace_first_buffer_logged = False
        session.seek_trace_player_play_logged = False
        session.seek_trace_elapsed_logged = False
        self._log_seek_trace(
            session,
            "requested",
            current_elapsed=round(session.last_elapsed_seconds, 3),
        )

    def _log_seek_trace(self, session, event, **payload):
        if not SEEK_TRACE_LOGGING or session.seek_trace_id == 0:
            return

        details = {
            "seek_id": session.seek_trace_id,
            "event": event,
            "ms": round((time.perf_counter() - session.seek_trace_started_at) * 1000, 1),
            "target": round(session.seek_trace_target, 3),
            "paused": bool(session.paused),
            "generation": session.decoder_generation,
        }
        details.update(payload)
        print("Seek trace", details)

    def _finish_seek_trace(self, session, event=None, **payload):
        if not SEEK_TRACE_LOGGING or session.seek_trace_id == 0:
            return

        if event is not None:
            self._log_seek_trace(session, event, **payload)

        session.seek_trace_id = 0
        session.seek_trace_started_at = 0.0
        session.seek_trace_target = 0.0
        session.seek_trace_first_chunk_logged = False
        session.seek_trace_first_buffer_logged = False
        session.seek_trace_player_play_logged = False
        session.seek_trace_elapsed_logged = False

    def _build_engine(self, session):
        engine = AVFoundation.AVAudioEngine.alloc().init()
        player = AVFoundation.AVAudioPlayerNode.alloc().init()
        audio_format = (
            AVFoundation.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
                float(INTERNAL_SAMPLE_RATE),
                CHANNELS,
            )
        )

        engine.attachNode_(player)
        mixer = engine.mainMixerNode()
        engine.connect_to_format_(player, mixer, audio_format)
        engine.outputNode()

        session.engine = engine
        session.player = player
        session.mixer = mixer
        session.format = audio_format

        self._register_engine_observer(session)
        self._install_visualizer_tap(session)

        engine.prepare()
        engine.startAndReturnError_(None)

    def _register_engine_observer(self, session):
        observer = EngineConfigurationObserver.alloc().initWithCallback_(
            lambda sid=session.id: self._enqueue_command("route_event", "engine_config", sid)
        )
        self._notification_center.addObserver_selector_name_object_(
            observer,
            "handleConfigChange:",
            AVFoundation.AVAudioEngineConfigurationChangeNotification,
            session.engine,
        )
        session.notification_observer = observer

    def _install_visualizer_tap(self, session):
        tap_format = session.mixer.outputFormatForBus_(0)

        def tap_block(buffer, when, sid=session.id):
            self._capture_visualizer_snapshot(sid, buffer)

        session.mixer.installTapOnBus_bufferSize_format_block_(
            0,
            VISUALIZER_TAP_BUFFER_FRAMES,
            tap_format,
            tap_block,
        )
        session.tap_block = tap_block

    def _start_decoder_thread(self, session):
        session.decoder_generation += 1
        generation = session.decoder_generation
        decoder_stop_event = threading.Event()
        session.decoder_stop_event = decoder_stop_event
        session.decoded_queue = queue.Queue(maxsize=DECODER_QUEUE_BUFFERS)
        session.decoder_thread = threading.Thread(
            target=self._decoder_loop,
            args=(session, generation, decoder_stop_event, session.decoded_queue),
            daemon=True,
        )
        session.decoder_thread.start()

    def _decoder_loop(self, session, generation, decoder_stop_event, decoded_queue):
        ytdlp_process = None
        ffmpeg_process = None

        try:
            if session.stop_event.is_set() or decoder_stop_event.is_set():
                return

            ffmpeg_cmd = ["ffmpeg"]
            if session.base_offset_seconds > 0:
                ffmpeg_cmd += ["-ss", str(session.base_offset_seconds)]
            if session.is_local:
                ffmpeg_cmd += [
                    "-i",
                    session.url,
                    "-f",
                    "f32le",
                    "-acodec",
                    "pcm_f32le",
                    "-ac",
                    str(CHANNELS),
                    "-ar",
                    str(INTERNAL_SAMPLE_RATE),
                    "-loglevel",
                    "error",
                    "-",
                ]
                ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            else:
                ytdlp_process = subprocess.Popen(
                    [
                        "yt-dlp",
                        "-f",
                        "bestaudio",
                        "-o",
                        "-",
                        "--no-warnings",
                        session.url,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                ffmpeg_cmd += [
                    "-i",
                    "pipe:0",
                    "-f",
                    "f32le",
                    "-acodec",
                    "pcm_f32le",
                    "-ac",
                    str(CHANNELS),
                    "-ar",
                    str(INTERNAL_SAMPLE_RATE),
                    "-loglevel",
                    "error",
                    "-",
                ]
                ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=ytdlp_process.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if ytdlp_process.stdout is not None:
                    ytdlp_process.stdout.close()

            session.ytdlp_process = ytdlp_process
            session.ffmpeg_process = ffmpeg_process

            bytes_per_chunk = PCM_BUFFER_FRAMES * PCM_BYTES_PER_FRAME
            while not session.stop_event.is_set() and not decoder_stop_event.is_set():
                data = ffmpeg_process.stdout.read(bytes_per_chunk)
                if not data:
                    break

                usable = len(data) - (len(data) % PCM_BYTES_PER_FRAME)
                if usable <= 0:
                    continue

                chunk = (
                    np.frombuffer(data[:usable], dtype=np.float32)
                    .reshape(-1, CHANNELS)
                    .copy()
                )
                if (
                    session.seek_trace_id != 0
                    and session.decoder_generation == generation
                    and not session.seek_trace_first_chunk_logged
                ):
                    session.seek_trace_first_chunk_logged = True
                    self._log_seek_trace(
                        session,
                        "first_pcm_chunk",
                        chunk_frames=int(len(chunk)),
                    )

                while not session.stop_event.is_set() and not decoder_stop_event.is_set():
                    try:
                        decoded_queue.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue

            if session.stop_event.is_set() or decoder_stop_event.is_set():
                return

            ffmpeg_code = self._wait_process(ffmpeg_process)
            ytdlp_code = self._wait_process(ytdlp_process)
            if ffmpeg_code not in (0, None):
                raise RuntimeError(f"ffmpeg exited with status {ffmpeg_code}")
            if ytdlp_code not in (0, None):
                raise RuntimeError(f"yt-dlp exited with status {ytdlp_code}")

            self._enqueue_command("decoder_eof", session.id, generation)
        except Exception as exc:
            if not session.stop_event.is_set() and not decoder_stop_event.is_set():
                log_exception("Decoder error", exc)
                self._enqueue_command("decoder_failed", session.id, generation, str(exc))
        finally:
            self._cleanup_process(ffmpeg_process)
            self._cleanup_process(ytdlp_process)
            if session.decoder_generation == generation:
                session.ffmpeg_process = None
                session.ytdlp_process = None

    def _service_session(self, session):
        self._refresh_elapsed(session)

        if session.rebuild_pending and time.monotonic() >= session.rebuild_deadline:
            request = self._resume_request(
                session,
                start_time=session.last_elapsed_seconds,
                retry_kind="route_change",
                retry_attempt=0,
            )
            self._discard_session(
                session,
                reason="route_change",
                clear_public_state=False,
                notify_stopped=False,
            )
            return self._attempt_start_request(request)

        self._schedule_available_buffers(session)

        if not session.paused and session.scheduled_frames_total > 0 and not session.started_playback:
            try:
                session.player.play()
                session.started_playback = True
                if session.seek_trace_id != 0 and not session.seek_trace_player_play_logged:
                    session.seek_trace_player_play_logged = True
                    self._log_seek_trace(
                        session,
                        "player_play_called",
                        scheduled_ahead_frames=int(self._scheduled_ahead_frames(session)),
                    )
                self._publish_state(active=True, starting=False, paused=False)
            except Exception as exc:
                log_exception("AVAudioPlayerNode play failed", exc)
                request = self._resume_request(
                    session,
                    start_time=session.last_elapsed_seconds,
                    retry_kind="route_change",
                    retry_attempt=0,
                )
                self._discard_session(
                    session,
                    reason="play_failed",
                    clear_public_state=False,
                    notify_stopped=False,
                )
                return self._attempt_start_request(request)

        self._refresh_elapsed(session)

        if self._should_finish_naturally(session):
            callback = session.on_finished
            self._discard_session(
                session,
                reason="finished",
                clear_public_state=True,
                notify_stopped=False,
            )
            if callback:
                callback()
            return None

        if self._should_stop_for_error(session):
            print(
                "Playback stopped due to decoder failure",
                {"error": session.decoder_error},
            )
            callback = session.on_stopped
            self._discard_session(
                session,
                reason="decoder_failed",
                clear_public_state=True,
                notify_stopped=False,
            )
            if callback:
                callback()
            return None

        return session

    def _schedule_available_buffers(self, session):
        while self._scheduled_ahead_frames(session) < SCHEDULE_AHEAD_FRAMES:
            try:
                chunk = session.decoded_queue.get_nowait()
            except queue.Empty:
                break

            if chunk is None or len(chunk) == 0:
                continue

            buffer = self._make_pcm_buffer(session.format, chunk)
            buffer_id = session.next_buffer_id
            session.next_buffer_id += 1
            session.scheduled_buffers[buffer_id] = buffer
            session.scheduled_frames_total += len(chunk)

            def completion_handler(
                callback_type,
                sid=session.id,
                bid=buffer_id,
            ):
                self._enqueue_command(
                    "buffer_complete",
                    sid,
                    bid,
                    int(callback_type),
                )

            try:
                session.player.scheduleBuffer_completionCallbackType_completionHandler_(
                    buffer,
                    AVFoundation.AVAudioPlayerNodeCompletionDataPlayedBack,
                    completion_handler,
                )
                if session.seek_trace_id != 0 and not session.seek_trace_first_buffer_logged:
                    session.seek_trace_first_buffer_logged = True
                    scheduled_ahead_frames = int(self._scheduled_ahead_frames(session))
                    self._log_seek_trace(
                        session,
                        "first_buffer_scheduled",
                        buffer_frames=int(len(chunk)),
                        scheduled_ahead_frames=scheduled_ahead_frames,
                    )
                    if session.paused:
                        self._finish_seek_trace(
                            session,
                            "paused_ready",
                            scheduled_ahead_frames=scheduled_ahead_frames,
                        )
            except Exception as exc:
                log_exception("scheduleBuffer failed", exc)
                session.rebuild_pending = True
                session.rebuild_deadline = time.monotonic()
                break

    def _set_paused(self, session, paused):
        self._refresh_elapsed(session)
        session.paused = paused

        if paused:
            try:
                session.player.pause()
            except Exception as exc:
                log_exception("AVAudioPlayerNode pause failed", exc)
            self._publish_state(active=True, starting=False, paused=True)
            return

        self._publish_state(active=True, starting=not session.started_playback, paused=False)
        if session.started_playback and self._scheduled_ahead_frames(session) > 0:
            try:
                session.player.play()
                self._publish_state(active=True, starting=False, paused=False)
            except Exception as exc:
                log_exception("AVAudioPlayerNode resume failed", exc)
                session.rebuild_pending = True
                session.rebuild_deadline = time.monotonic()

    def _scheduled_ahead_frames(self, session):
        return max(0, session.scheduled_frames_total - session.last_rendered_frames)

    def _refresh_elapsed(self, session):
        elapsed = session.last_elapsed_seconds

        if session.player is not None and session.started_playback:
            try:
                render_time = session.player.lastRenderTime()
                if render_time is not None:
                    player_time = session.player.playerTimeForNodeTime_(render_time)
                else:
                    player_time = None

                if player_time is not None and player_time.isSampleTimeValid():
                    sample_time = max(0, int(player_time.sampleTime()))
                    session.last_rendered_frames = max(
                        session.last_rendered_frames,
                        sample_time,
                    )
                    elapsed = session.base_offset_seconds + (
                        session.last_rendered_frames / INTERNAL_SAMPLE_RATE
                    )
                    if (
                        session.seek_trace_id != 0
                        and not session.seek_trace_elapsed_logged
                        and session.last_rendered_frames > 0
                    ):
                        session.seek_trace_elapsed_logged = True
                        self._finish_seek_trace(
                            session,
                            "first_elapsed_advance",
                            rendered_frames=int(session.last_rendered_frames),
                            elapsed=round(elapsed, 3),
                        )
                else:
                    elapsed = max(elapsed, session.base_offset_seconds)
            except Exception:
                elapsed = max(elapsed, session.base_offset_seconds)
        else:
            elapsed = max(elapsed, session.base_offset_seconds)

        if session.duration > 0:
            elapsed = min(elapsed, session.duration)

        session.last_elapsed_seconds = elapsed
        self._publish_state(elapsed=elapsed)

    def _should_finish_naturally(self, session):
        if not session.decoder_eof or session.decoder_failed or session.rebuild_pending:
            return False
        if not session.decoded_queue.empty():
            return False
        if session.scheduled_frames_total == 0:
            return False

        tolerance = PCM_BUFFER_FRAMES // 2
        return session.last_rendered_frames + tolerance >= session.scheduled_frames_total

    def _should_stop_for_error(self, session):
        if not session.decoder_failed:
            return False
        if not session.decoded_queue.empty():
            return False
        if session.scheduled_frames_total == 0:
            return True

        tolerance = PCM_BUFFER_FRAMES // 2
        return session.last_rendered_frames + tolerance >= session.scheduled_frames_total

    @staticmethod
    def _resume_request(
        session,
        *,
        start_time=None,
        retry_kind=None,
        retry_attempt=0,
    ):
        if start_time is None:
            start_time = session.last_elapsed_seconds
        return PlayRequest(
            url=session.url,
            duration=session.duration,
            is_local=session.is_local,
            start_time=start_time,
            paused=session.paused,
            on_finished=session.on_finished,
            on_stopped=session.on_stopped,
            retry_kind=retry_kind,
            retry_attempt=retry_attempt,
        )

    def _restart_local_decoder(self, session, start_time):
        if session.player is None or session.engine is None:
            raise RuntimeError("Local seek requires an active player node")

        try:
            session.player.stop()
        except Exception as exc:
            log_exception("AVAudioPlayerNode stop failed during seek", exc)
            raise

        self._stop_decoder(session, fast=True)
        session.base_offset_seconds = max(0.0, float(start_time))
        session.last_elapsed_seconds = session.base_offset_seconds
        session.last_rendered_frames = 0
        session.decoder_eof = False
        session.decoder_failed = False
        session.decoder_error = None
        session.scheduled_buffers.clear()
        session.scheduled_frames_total = 0
        session.started_playback = False
        session.rebuild_pending = False
        session.rebuild_deadline = 0.0
        self._publish_state(
            active=True,
            starting=not session.paused,
            paused=session.paused,
            is_local=True,
            duration=session.duration,
            elapsed=session.base_offset_seconds,
            reset_grid=True,
        )
        session.engine.prepare()
        self._start_decoder_thread(session)
        self._log_seek_trace(
            session,
            "decoder_restarted",
            start_time=round(session.base_offset_seconds, 3),
        )

    def _stop_decoder(self, session, fast=False):
        if session.decoder_stop_event is not None:
            session.decoder_stop_event.set()

        cleanup_timeout = 0.1 if fast else 2.0
        self._cleanup_process(
            session.ffmpeg_process,
            timeout=cleanup_timeout,
            force_kill=fast,
        )
        self._cleanup_process(
            session.ytdlp_process,
            timeout=cleanup_timeout,
            force_kill=fast,
        )

        if session.decoder_thread is not None and session.decoder_thread.is_alive():
            session.decoder_thread.join(timeout=0.25 if fast else 1.0)

        session.decoder_thread = None
        session.decoder_stop_event = None
        session.ffmpeg_process = None
        session.ytdlp_process = None

    def _discard_session(
        self,
        session,
        *,
        reason,
        clear_public_state=True,
        notify_stopped=False,
    ):
        session.stop_event.set()

        if session.mixer is not None:
            try:
                session.mixer.removeTapOnBus_(0)
            except Exception:
                pass

        if session.notification_observer is not None:
            try:
                self._notification_center.removeObserver_(session.notification_observer)
            except Exception:
                pass

        if session.player is not None:
            try:
                session.player.stop()
            except Exception:
                pass

        if session.seek_trace_id != 0:
            self._finish_seek_trace(session, "discarded", reason=reason)

        if session.engine is not None:
            try:
                session.engine.stop()
            except Exception:
                pass

        self._stop_decoder(session)

        if clear_public_state:
            self._publish_stopped()
        else:
            self._publish_state(session_id=0, elapsed=session.last_elapsed_seconds)

        if notify_stopped and session.on_stopped:
            session.on_stopped()

        print("Session discarded", {"reason": reason, "session_id": session.id})

    @staticmethod
    def _make_pcm_buffer(audio_format, interleaved_chunk):
        frame_count = int(len(interleaved_chunk))
        buffer = AVFoundation.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            audio_format,
            frame_count,
        )
        left = np.ascontiguousarray(interleaved_chunk[:, 0], dtype=np.float32)
        right = np.ascontiguousarray(interleaved_chunk[:, 1], dtype=np.float32)

        channel_data = buffer.floatChannelData()
        left_dest = np.frombuffer(
            channel_data[0].as_buffer(frame_count),
            dtype=np.float32,
            count=frame_count,
        )
        right_dest = np.frombuffer(
            channel_data[1].as_buffer(frame_count),
            dtype=np.float32,
            count=frame_count,
        )
        left_dest[:] = left
        right_dest[:] = right
        buffer.setFrameLength_(frame_count)
        return buffer

    def _capture_visualizer_snapshot(self, session_id, buffer):
        with self._lock:
            if session_id != self._current_session_id:
                return

        try:
            frame_length = min(int(buffer.frameLength()), VISUALIZER_SNAPSHOT_FRAMES)
            if frame_length <= 0:
                return

            channel_data = buffer.floatChannelData()
            left = np.frombuffer(
                channel_data[0].as_buffer(frame_length),
                dtype=np.float32,
                count=frame_length,
            ).copy()
            right = np.frombuffer(
                channel_data[1].as_buffer(frame_length),
                dtype=np.float32,
                count=frame_length,
            ).copy()
            stereo = np.column_stack((left, right))

            with self._lock:
                if session_id == self._current_session_id:
                    self._viz_snapshot = stereo
        except Exception:
            pass

    def _compute_stereometer(self, stereo):
        left = stereo[:, 0]
        right = stereo[:, 1]

        mid = (left + right) * 0.5
        side = (left - right) * 0.5

        rms = float(np.sqrt(np.mean(mid**2 + side**2)))
        if rms > self._rms_peak:
            self._rms_peak = rms
        else:
            self._rms_peak *= 0.999
        self._rms_peak = max(self._rms_peak, 0.001)
        scale = 0.9 / self._rms_peak

        mid_scaled = mid * scale
        side_scaled = side * scale
        self._dot_grid *= self._dot_decay

        energy = mid**2 + side**2
        step = max(1, len(mid) // 80)
        indices = np.argsort(energy)[::-1][: len(mid) // step]

        for idx in indices:
            m = float(np.clip(mid_scaled[idx], -1, 1))
            s = float(np.clip(side_scaled[idx], -1, 1))
            x = int((s + 1.0) * 0.5 * (GRID_W - 1) + 0.5)
            y = int((m + 1.0) * 0.5 * (GRID_H - 1) + 0.5)
            x = max(0, min(GRID_W - 1, x))
            y = max(0, min(GRID_H - 1, y))
            self._dot_grid[x, y] = min(1.0, self._dot_grid[x, y] + 0.15)

    def _install_default_output_listener(self):
        if _AUDIO_OBJECT_ADD_PROPERTY_LISTENER is None:
            return

        @_AUDIO_OBJECT_LISTENER_PROC
        def _listener(in_object_id, in_number_addresses, in_addresses, in_client_data):
            self._enqueue_command("route_event", "default_output", None)
            return 0

        status = _AUDIO_OBJECT_ADD_PROPERTY_LISTENER(
            AUDIO_OBJECT_SYSTEM_OBJECT,
            byref(self._output_listener_address),
            _listener,
            None,
        )
        if status == 0:
            self._output_listener_callback = _listener
            self._output_listener_registered = True
        else:
            print(f"Failed to register default output listener: {status}")

    def _remove_default_output_listener(self):
        if (
            not self._output_listener_registered
            or _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER is None
            or self._output_listener_callback is None
        ):
            return

        status = _AUDIO_OBJECT_REMOVE_PROPERTY_LISTENER(
            AUDIO_OBJECT_SYSTEM_OBJECT,
            byref(self._output_listener_address),
            self._output_listener_callback,
            None,
        )
        if status != 0:
            print(f"Failed to remove default output listener: {status}")
        self._output_listener_registered = False
        self._output_listener_callback = None

    @staticmethod
    def _wait_process(proc):
        if proc is None:
            return None
        try:
            return proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            return proc.poll()

    @staticmethod
    def _cleanup_process(proc, timeout=2.0, force_kill=False):
        if proc and proc.poll() is None:
            try:
                if force_kill:
                    proc.kill()
                else:
                    proc.terminate()
                proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=timeout)
                except subprocess.TimeoutExpired:
                    pass


def grid_to_braille(grid):
    chars = []
    num_chars = GRID_W // 2
    for ch in range(num_chars):
        bits = 0
        for col in range(2):
            gx = ch * 2 + col
            for row in range(GRID_H):
                if grid[gx, row] > 0.15:
                    bits |= DOT_BITS[col][row]
        chars.append(chr(BRAILLE_BASE + bits))
    return "".join(chars)


def format_time(seconds):
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}"
    return f"{m}:{s:02d}"


def parse_duration(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _set_header_title(menu_item, primary, trailing=None):
    """Render a header menu-item at secondaryLabelColor brightness."""
    text = primary + ("  " + trailing if trailing else "")
    s = Foundation.NSMutableAttributedString.alloc().initWithString_(text)
    s.addAttribute_value_range_(
        AppKit.NSForegroundColorAttributeName,
        AppKit.NSColor.secondaryLabelColor(),
        Foundation.NSRange(0, s.length()),
    )
    menu_item._menuitem.setAttributedTitle_(s)


def progress_bar(elapsed, duration, width=20):
    if duration is None or duration <= 0:
        return f"{format_time(elapsed)}"
    clamped = max(0, min(elapsed, duration))
    if width <= 0:
        return f"{format_time(clamped)} / {format_time(duration)}"
    frac = clamped / duration
    playhead_pos = round(frac * (width - 1))
    bar = "━" * playhead_pos + "●" + "─" * (width - 1 - playhead_pos)
    return f"{bar}  {format_time(clamped)} / {format_time(duration)}"


def load_media_player_support():
    try:
        bundle = objc.loadBundle(
            "MediaPlayer",
            globals(),
            bundle_path=MEDIA_PLAYER_FRAMEWORK_PATH,
        )
        variables = {}
        objc.loadBundleVariables(
            bundle,
            variables,
            [
                ("MPNowPlayingInfoPropertyElapsedPlaybackTime", b"@"),
                ("MPNowPlayingInfoPropertyPlaybackRate", b"@"),
                ("MPMediaItemPropertyTitle", b"@"),
                ("MPMediaItemPropertyPlaybackDuration", b"@"),
            ],
        )
        return MediaPlayerSupport(
            command_center_class=objc.lookUpClass("MPRemoteCommandCenter"),
            now_playing_info_center_class=objc.lookUpClass("MPNowPlayingInfoCenter"),
            command_status_success=MP_REMOTE_COMMAND_STATUS_SUCCESS,
            command_status_command_failed=MP_REMOTE_COMMAND_STATUS_COMMAND_FAILED,
            command_status_no_such_content=MP_REMOTE_COMMAND_STATUS_NO_SUCH_CONTENT,
            property_elapsed_playback_time=str(
                variables["MPNowPlayingInfoPropertyElapsedPlaybackTime"]
            ),
            property_playback_rate=str(
                variables["MPNowPlayingInfoPropertyPlaybackRate"]
            ),
            property_title=str(variables["MPMediaItemPropertyTitle"]),
            property_playback_duration=str(
                variables["MPMediaItemPropertyPlaybackDuration"]
            ),
        )
    except Exception as exc:
        log_exception("MediaPlayer integration unavailable", exc)
        return None


class YTBar(rumps.App):
    def __init__(self):
        super().__init__("yt-bar", title=None)

        self.title = "⠆⣿⠰"
        self._idle_title = "⠆⣿⠰"
        self._state_lock = threading.RLock()

        self.engine = AudioEngine()
        self._tracks: list[TrackInfo] = []
        self._current_index = -1
        self._current_item: ResolvedItem | None = None
        self._current_playback_mode = "stream"
        self._current_item_generation = 0
        self._pending_actions = deque()
        self._recent_dirty = False
        self._recent_entries: dict[str, RecentItem] = {}
        self._item_last_played: dict[str, float] = {}
        self._scheduled_cache_track_ids: set[str] = set()
        self._cache_jobs: queue.Queue = queue.Queue()
        self._cache_shutdown = threading.Event()
        self._cache_workers = []
        self._cache_delay_timer = None
        self._recent_menu_observer = None
        self._cleanup_done = False
        self._media_player_support: MediaPlayerSupport | None = None
        self._remote_command_center = None
        self._now_playing_info_center = None
        self._remote_command_bridge = None
        self._remote_commands: list[object] = []

        self._ensure_cache_dir()
        self._cleanup_partial_cache_files()
        self._load_recent_index()
        self._sweep_stale_recent_entries()
        rumps.events.before_quit.register(self._cleanup_before_quit)

        self._now_playing = rumps.MenuItem("Not Playing")
        self._now_playing.set_callback(lambda *_: None)

        self._progress = rumps.MenuItem("")
        self._progress.set_callback(lambda *_: None)

        self._seek_menu = rumps.MenuItem("Seek")
        self._seek_items = []
        for i in range(10):
            pct = i * 10
            item = rumps.MenuItem(
                self._seek_label(i, -1),
                callback=lambda _, p=pct: self._seek_to_pct(p),
            )
            self._seek_items.append(item)
            self._seek_menu[f"seek_{pct}"] = item

        self._playpause_item = rumps.MenuItem(
            "Play / Pause", callback=self.on_playpause
        )
        self._recent_menu = rumps.MenuItem("Recent")
        self._paste_item = rumps.MenuItem(
            "Play from Clipboard", callback=self.on_paste_url
        )

        self.menu = [
            self._now_playing,
            self._progress,
            None,
            self._playpause_item,
            self._seek_menu,
            None,
            self._recent_menu,
            self._paste_item,
        ]

        self._rebuild_recent_menu()
        self._install_recent_menu_delegate()
        self._start_cache_workers()
        self._setup_media_player()

        self._viz_timer, self._viz_timer_target = _schedule_common_mode_timer(
            0.07, self._update_viz
        )
        self._progress_timer, self._progress_timer_target = _schedule_common_mode_timer(
            1.0, self._update_progress
        )

    @staticmethod
    def _seek_label(index, current_segment):
        pct = index * 10
        marker = "●" if index == current_segment else "○"
        return f"  {marker} {pct}%"

    @staticmethod
    def _clamp_start_time(duration, start_time):
        if duration <= 0:
            return max(0.0, start_time)
        max_start = max(0.0, duration - 0.25)
        return max(0.0, min(start_time, max_start))

    def _current_track(self):
        with self._state_lock:
            if 0 <= self._current_index < len(self._tracks):
                return self._tracks[self._current_index]
        return None

    def _current_track_snapshot(self):
        with self._state_lock:
            if 0 <= self._current_index < len(self._tracks):
                return self._current_index, self._tracks[self._current_index]
        return -1, None

    def _enqueue_ui_action(self, action, *payload):
        with self._state_lock:
            self._pending_actions.append((action, *payload))

    def _ensure_cache_dir(self):
        os.makedirs(SONGS_DIR, exist_ok=True)

    def _cleanup_partial_cache_files(self):
        for name in os.listdir(SONGS_DIR):
            if name.endswith(PARTIAL_CACHE_SUFFIX):
                path = os.path.join(SONGS_DIR, name)
                try:
                    os.remove(path)
                except OSError:
                    pass

    def _start_cache_workers(self):
        for _ in range(CACHE_WORKER_COUNT):
            worker = threading.Thread(target=self._cache_worker_loop, daemon=True)
            worker.start()
            self._cache_workers.append(worker)

    def _load_recent_index(self):
        if not os.path.exists(RECENT_INDEX_PATH):
            return

        try:
            with open(RECENT_INDEX_PATH, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log_exception("Failed to load recent index", exc)
            return

        if not isinstance(payload, list):
            return

        entries = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            recent = RecentItem.from_dict(item)
            if recent.tracks:
                entries[recent.cache_key] = recent
        with self._state_lock:
            self._recent_entries = entries

    def _save_recent_index_locked(self):
        payload = [
            entry.to_dict()
            for entry in sorted(
                self._recent_entries.values(),
                key=lambda entry: entry.last_played,
                reverse=True,
            )
        ]
        tmp_path = f"{RECENT_INDEX_PATH}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
        os.replace(tmp_path, RECENT_INDEX_PATH)

    def _install_recent_menu_delegate(self):
        if self._recent_menu._menu is None:
            return
        observer = RecentMenuObserver.alloc().initWithCallback_(
            self._on_recent_menu_will_open
        )
        self._recent_menu._menu.setDelegate_(observer)
        self._recent_menu_observer = observer

    def _mark_recent_dirty_locked(self):
        self._recent_dirty = True

    def _sweep_stale_recent_entries_locked(self):
        changed = False
        for key, entry in list(self._recent_entries.items()):
            valid_tracks = [track for track in entry.tracks if track.is_cached()]
            if not valid_tracks:
                del self._recent_entries[key]
                changed = True
                continue
            if len(valid_tracks) != len(entry.tracks):
                entry.tracks = valid_tracks
                changed = True
        if changed:
            self._save_recent_index_locked()
            self._mark_recent_dirty_locked()
        return changed

    def _sweep_stale_recent_entries(self):
        with self._state_lock:
            return self._sweep_stale_recent_entries_locked()

    def _recent_entries_for_menu(self):
        with self._state_lock:
            entries = sorted(
                self._recent_entries.values(),
                key=lambda entry: entry.last_played,
                reverse=True,
            )
        return entries[:RECENT_MENU_LIMIT]

    def _rebuild_recent_menu(self):
        entries = self._recent_entries_for_menu()
        if self._recent_menu._menu is not None:
            self._recent_menu.clear()

        if not entries:
            placeholder = rumps.MenuItem("No recent items")
            placeholder.set_callback(None)
            self._recent_menu["recent_empty"] = placeholder
            self._install_recent_menu_delegate()
            return

        for index, entry in enumerate(entries):
            item = rumps.MenuItem(
                truncate_title(entry.title),
                callback=lambda _, key=entry.cache_key: self._play_recent_entry(key),
            )
            self._recent_menu[f"recent_{index}"] = item

        self._install_recent_menu_delegate()

    def _on_recent_menu_will_open(self):
        self._sweep_stale_recent_entries()
        self._rebuild_recent_menu()

    def _update_seek_markers(self, elapsed=0, duration=0):
        current_segment = None
        if duration > 0:
            frac = max(0, min(elapsed, duration)) / duration
            current_segment = min(9, int(frac * len(self._seek_items)))
        for index, item in enumerate(self._seek_items):
            item.title = self._seek_label(index, current_segment)

    def _set_progress_display(self, elapsed=None, duration=None):
        if elapsed is None:
            if not self.engine.is_active:
                _set_header_title(self._progress, "")
                self._update_seek_markers()
                return
            elapsed = self.engine.elapsed
        if duration is None:
            duration = self.engine.duration
        _set_header_title(
            self._progress,
            progress_bar(elapsed, duration, width=PROGRESS_BAR_WIDTH),
        )
        self._update_seek_markers(elapsed, duration)

    def _seek_to_pct(self, pct):
        if self.engine.duration <= 0:
            return
        target_sec = int(self.engine.duration * pct / 100)
        self._seek_current_track_to(target_sec)

    def _play_or_resume_current_track(self):
        if self.engine.is_active:
            if self.engine.is_paused:
                self.engine.toggle_pause()
                self._sync_now_playing_info()
            return True

        current_index, track = self._current_track_snapshot()
        if track is None:
            return False
        self._play_track(current_index)
        return True

    def _pause_current_track(self):
        if not self.engine.is_active:
            return False
        if not self.engine.is_paused:
            self.engine.toggle_pause()
            self._sync_now_playing_info()
        return True

    def _toggle_play_pause(self):
        if self.engine.is_active:
            self.engine.toggle_pause()
            self._sync_now_playing_info()
            return True
        return self._play_or_resume_current_track()

    def _seek_current_track_to(self, target_sec):
        current_index, track = self._current_track_snapshot()
        if track is None or not self.engine.is_active:
            return False

        duration = self.engine.duration
        if duration <= 0:
            return False

        paused = self.engine.is_paused
        clamped = self._clamp_start_time(duration, target_sec)
        if self.engine.seek_current(clamped):
            self._set_progress_display(
                elapsed=clamped,
                duration=duration,
            )
            self._sync_now_playing_info()
            return True

        self._play_track(current_index, start_time=clamped, paused=paused)
        return True

    def _seek_current_track_by(self, delta_seconds):
        if self.engine.duration <= 0:
            return False
        return self._seek_current_track_to(self.engine.elapsed + delta_seconds)

    def _handle_stopped_ui(self):
        if self.engine.is_active:
            return
        _set_header_title(self._now_playing, "Not Playing")
        self._set_progress_display()
        self._clear_now_playing_info()

    def _perform_ui_action(self, action, *payload):
        if action == "play":
            self._play_or_resume_current_track()
            return
        if action == "stopped":
            self._handle_stopped_ui()
            return
        if action == "remote_play":
            self._play_or_resume_current_track()
            return
        if action == "remote_pause":
            self._pause_current_track()
            return
        if action == "remote_toggle":
            self._toggle_play_pause()
            return
        if action == "remote_seek_delta":
            self._seek_current_track_by(payload[0])

    def _remote_command_status_success(self):
        support = self._media_player_support
        return 0 if support is None else support.command_status_success

    def _remote_command_status_command_failed(self):
        support = self._media_player_support
        return 0 if support is None else support.command_status_command_failed

    def _remote_command_status_no_such_content(self):
        support = self._media_player_support
        return 0 if support is None else support.command_status_no_such_content

    def _handle_remote_play_command(self):
        current_index, track = self._current_track_snapshot()
        if self.engine.is_active:
            if self.engine.is_paused:
                self._enqueue_ui_action("remote_play")
            return self._remote_command_status_success()
        if track is None or current_index < 0:
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action("remote_play")
        return self._remote_command_status_success()

    def _handle_remote_pause_command(self):
        if not self.engine.is_active:
            return self._remote_command_status_no_such_content()
        if not self.engine.is_paused:
            self._enqueue_ui_action("remote_pause")
        return self._remote_command_status_success()

    def _handle_remote_toggle_command(self):
        current_index, track = self._current_track_snapshot()
        if not self.engine.is_active and (track is None or current_index < 0):
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action("remote_toggle")
        return self._remote_command_status_success()

    def _handle_remote_skip_forward_command(self):
        if not self.engine.is_active or self.engine.duration <= 0:
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action("remote_seek_delta", REMOTE_SKIP_INTERVAL_SECONDS)
        return self._remote_command_status_success()

    def _handle_remote_skip_backward_command(self):
        if not self.engine.is_active or self.engine.duration <= 0:
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action("remote_seek_delta", -REMOTE_SKIP_INTERVAL_SECONDS)
        return self._remote_command_status_success()

    def _setup_media_player(self):
        support = load_media_player_support()
        if support is None:
            return

        try:
            self._media_player_support = support
            self._remote_command_center = (
                support.command_center_class.sharedCommandCenter()
            )
            self._now_playing_info_center = (
                support.now_playing_info_center_class.defaultCenter()
            )
            self._remote_command_bridge = (
                RemoteCommandBridge.alloc().initWithOwner_(self)
            )
            self._register_remote_commands()
        except Exception as exc:
            log_exception("Failed to initialize MediaPlayer integration", exc)
            self._unregister_remote_commands()
            self._media_player_support = None
            self._remote_command_center = None
            self._now_playing_info_center = None
            self._remote_command_bridge = None

    def _register_remote_commands(self):
        center = self._remote_command_center
        bridge = self._remote_command_bridge
        if center is None or bridge is None:
            return

        commands = [
            (center.playCommand(), "handlePlayCommand:"),
            (center.pauseCommand(), "handlePauseCommand:"),
            (center.togglePlayPauseCommand(), "handleTogglePlayPauseCommand:"),
            (center.skipForwardCommand(), "handleSkipForwardCommand:"),
            (center.skipBackwardCommand(), "handleSkipBackwardCommand:"),
            (center.nextTrackCommand(), "handleNextTrackCommand:"),
            (center.previousTrackCommand(), "handlePreviousTrackCommand:"),
        ]
        center.skipForwardCommand().setPreferredIntervals_(
            [REMOTE_SKIP_INTERVAL_SECONDS]
        )
        center.skipBackwardCommand().setPreferredIntervals_(
            [REMOTE_SKIP_INTERVAL_SECONDS]
        )
        center.nextTrackCommand().setEnabled_(True)
        center.previousTrackCommand().setEnabled_(True)

        for command, selector in commands:
            command.addTarget_action_(bridge, selector)
            self._remote_commands.append(command)

    def _unregister_remote_commands(self):
        bridge = self._remote_command_bridge
        if bridge is None:
            self._remote_commands.clear()
            return
        for command in self._remote_commands:
            try:
                command.removeTarget_(bridge)
            except Exception:
                pass
        self._remote_commands.clear()

    def _sync_now_playing_info(self):
        support = self._media_player_support
        center = self._now_playing_info_center
        if support is None or center is None:
            return

        track = self._current_track()
        if track is None or not self.engine.is_active:
            self._clear_now_playing_info()
            return

        info = {
            support.property_title: track.title,
            support.property_elapsed_playback_time: float(self.engine.elapsed),
            support.property_playback_rate: 0.0 if self.engine.is_paused else 1.0,
        }
        duration = self.engine.duration or track.duration
        if duration > 0:
            info[support.property_playback_duration] = float(duration)

        try:
            center.setNowPlayingInfo_(info)
        except Exception as exc:
            log_exception("Failed to update now playing info", exc)

    def _clear_now_playing_info(self):
        center = self._now_playing_info_center
        if center is None:
            return
        try:
            center.setNowPlayingInfo_(None)
        except Exception as exc:
            log_exception("Failed to clear now playing info", exc)

    def _update_viz(self, _):
        pending_actions = []
        rebuild_recent = False
        with self._state_lock:
            if self._pending_actions:
                pending_actions = list(self._pending_actions)
                self._pending_actions.clear()
            if self._recent_dirty:
                rebuild_recent = True
                self._recent_dirty = False

        if rebuild_recent:
            self._rebuild_recent_menu()

        for action in pending_actions:
            self._perform_ui_action(*action)

        if self.engine.is_playing:
            self.title = grid_to_braille(self.engine.dot_grid)
        elif self.engine.is_active:
            self.title = "⣿⣿"
        elif self.title != self._idle_title:
            self.title = self._idle_title

    def _update_progress(self, _):
        self._set_progress_display()

    def _on_track_finished(self):
        with self._state_lock:
            if self._current_index + 1 < len(self._tracks):
                self._current_index += 1
                self._pending_actions.append(("play",))
            else:
                self._pending_actions.append(("stopped",))

    def _on_engine_stopped(self):
        self._enqueue_ui_action("stopped")

    def _play_track(self, index, start_time=0, paused=False):
        with self._state_lock:
            if index < 0 or index >= len(self._tracks):
                return
            track = self._tracks[index]
            playback_mode = self._current_playback_mode
            self._current_index = index

        start_time = self._clamp_start_time(track.duration, start_time)
        badge = "◌" if playback_mode == "stream" else "●"
        _set_header_title(self._now_playing, track.title, trailing=badge)

        if playback_mode == "local" and track.local_path:
            source = track.absolute_local_path
            is_local = True
        else:
            source = track.source_url
            is_local = False

        self.engine.play(
            source,
            on_finished=self._on_track_finished,
            on_stopped=self._on_engine_stopped,
            duration=track.duration,
            is_local=is_local,
            start_time=start_time,
            paused=paused,
        )
        self._set_progress_display(
            elapsed=start_time,
            duration=track.duration,
        )
        self._sync_now_playing_info()

    def _is_playlist_url(self, url):
        return "list=" in url or "/playlist" in url

    def _run_yt_dlp_json(self, args, timeout):
        try:
            result = subprocess.run(
                args,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            print(f"yt-dlp timed out while resolving {args[-1]}")
            return None

        if result.returncode != 0:
            error_text = result.stderr.strip() or "unknown yt-dlp failure"
            print(f"yt-dlp failed for {args[-1]}: {error_text}")
            return None

        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            log_exception("yt-dlp JSON parse failure", exc)
            return None

    def _track_from_info(self, info, fallback_url, default_title="Unknown"):
        source_url = default_source_url(info, fallback_url).strip()
        if not source_url:
            return None

        raw_id = str(info.get("id") or stable_hash(source_url))
        track_id = sanitize_cache_key(raw_id)
        return TrackInfo(
            id=track_id,
            title=(info.get("title") or default_title).strip() or default_title,
            duration=parse_duration(info.get("duration")),
            source_url=source_url,
            local_path=cache_relpath_for_id(track_id),
        )

    def _resolve_playlist(self, url):
        info = self._run_yt_dlp_json(
            ["yt-dlp", "-J", "--flat-playlist", "--no-warnings", url],
            timeout=60,
        )
        if not isinstance(info, dict):
            return None

        tracks = []
        for index, entry in enumerate(info.get("entries") or []):
            if not isinstance(entry, dict):
                continue
            track = self._track_from_info(
                entry,
                url,
                default_title=f"Track {index + 1}",
            )
            if track is None:
                continue
            tracks.append(track)

        if not tracks:
            print(f"yt-dlp returned no playlist entries for {url}")
            return None

        playlist_id = sanitize_cache_key(str(info.get("id") or stable_hash(url)))
        return ResolvedItem(
            kind="playlist",
            id=playlist_id,
            title=(info.get("title") or "Playlist").strip() or "Playlist",
            source_url=default_source_url(info, url) or url,
            tracks=tracks,
        )

    def _resolve_single(self, url):
        info = self._run_yt_dlp_json(
            ["yt-dlp", "-J", "--no-playlist", "--no-warnings", url],
            timeout=30,
        )
        if not isinstance(info, dict):
            return None

        track = self._track_from_info(info, url)
        if track is None:
            return None

        return ResolvedItem(
            kind="video",
            id=track.id,
            title=track.title,
            source_url=track.source_url,
            tracks=[track],
        )

    def _resolve_url(self, url):
        if self._is_playlist_url(url):
            item = self._resolve_playlist(url)
            if item is not None:
                return item
        return self._resolve_single(url)

    def _get_clipboard(self):
        pb = AppKit.NSPasteboard.generalPasteboard()
        return pb.stringForType_(AppKit.NSStringPboardType) or ""

    def _latest_last_played_locked(self, item_key):
        return self._item_last_played.get(item_key, time.time())

    def _refresh_recent_from_item_locked(
        self,
        item,
        *,
        last_played=None,
        remove_if_empty=False,
    ):
        cached_tracks = item.cached_tracks()
        item_key = item.cache_key
        existing = self._recent_entries.get(item_key)
        effective_last_played = (
            last_played
            if last_played is not None
            else (
                existing.last_played
                if existing is not None
                else self._latest_last_played_locked(item_key)
            )
        )

        if item.kind == "video" and cached_tracks:
            cached_tracks = [cached_tracks[0]]

        if not cached_tracks:
            if remove_if_empty and existing is not None:
                del self._recent_entries[item_key]
                self._save_recent_index_locked()
                self._mark_recent_dirty_locked()
                return True
            return False

        updated = RecentItem(
            kind=item.kind,
            id=item.id,
            title=item.title,
            source_url=item.source_url,
            last_played=effective_last_played,
            tracks=cached_tracks,
        )

        if existing is not None and existing.to_dict() == updated.to_dict():
            return False

        self._recent_entries[item_key] = updated
        self._save_recent_index_locked()
        self._mark_recent_dirty_locked()
        return True

    def _record_item_played(self, item, *, last_played=None):
        timestamp = time.time() if last_played is None else last_played
        with self._state_lock:
            self._item_last_played[item.cache_key] = timestamp
            self._refresh_recent_from_item_locked(
                item,
                last_played=timestamp,
                remove_if_empty=False,
            )

    def _cancel_cache_delay_timer_locked(self):
        timer = self._cache_delay_timer
        self._cache_delay_timer = None
        if timer is not None:
            timer.cancel()

    def _enqueue_cache_jobs_for_item(self, item):
        with self._state_lock:
            self._refresh_recent_from_item_locked(
                item,
                last_played=self._latest_last_played_locked(item.cache_key),
                remove_if_empty=False,
            )
            for track in item.tracks:
                if track.is_cached() or not track.source_url:
                    continue
                if track.id in self._scheduled_cache_track_ids:
                    continue
                self._scheduled_cache_track_ids.add(track.id)
                self._cache_jobs.put(CacheJob(item=item, track=track))

    def _schedule_delayed_cache(self, item, generation):
        with self._state_lock:
            self._cancel_cache_delay_timer_locked()
            timer = threading.Timer(
                CACHE_DELAY_SECONDS,
                lambda: self._start_cache_if_still_current(item, generation),
            )
            timer.daemon = True
            self._cache_delay_timer = timer
            timer.start()

    def _start_cache_if_still_current(self, item, generation):
        with self._state_lock:
            is_current_item = (
                generation == self._current_item_generation
                and self._current_item is not None
                and self._current_item.cache_key == item.cache_key
                and self._current_playback_mode == "stream"
            )
        if not is_current_item or not (self.engine.is_active or self.engine.is_paused):
            return
        self._enqueue_cache_jobs_for_item(item)

    def _download_track_cache(self, track):
        final_path = track.absolute_local_path
        partial_path = track.partial_local_path
        output_template = os.path.join(SONGS_DIR, f"{track.id}.partial.%(ext)s")

        if os.path.exists(final_path):
            return True

        try:
            os.remove(partial_path)
        except OSError:
            pass

        result = subprocess.run(
            [
                "yt-dlp",
                "--no-warnings",
                "--no-playlist",
                "-x",
                "--audio-format",
                "opus",
                "-o",
                output_template,
                track.source_url,
            ],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            error_text = result.stderr.strip() or "unknown yt-dlp failure"
            print(f"cache download failed for {track.source_url}: {error_text}")
            try:
                os.remove(partial_path)
            except OSError:
                pass
            return False

        if os.path.exists(partial_path):
            os.replace(partial_path, final_path)

        if not os.path.exists(final_path):
            print(f"cache download produced no file for {track.source_url}")
            return False

        return True

    def _cache_worker_loop(self):
        while not self._cache_shutdown.is_set():
            try:
                job = self._cache_jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            if job is None:
                self._cache_jobs.task_done()
                break

            try:
                if self._download_track_cache(job.track):
                    with self._state_lock:
                        self._refresh_recent_from_item_locked(
                            job.item,
                            last_played=self._latest_last_played_locked(job.item.cache_key),
                            remove_if_empty=False,
                        )
            finally:
                with self._state_lock:
                    self._scheduled_cache_track_ids.discard(job.track.id)
                self._cache_jobs.task_done()

    def _start_item_playback(self, item, *, playback_mode):
        last_played = time.time()
        self.engine.stop()

        with self._state_lock:
            self._current_item = item
            self._tracks = list(item.tracks)
            self._current_index = 0
            # Do not reroute a live stream to local mid-track. Cache only affects future plays.
            self._current_playback_mode = playback_mode
            self._current_item_generation += 1
            generation = self._current_item_generation
            self._pending_actions.append(("play",))

        self._record_item_played(item, last_played=last_played)
        if playback_mode == "stream":
            self._schedule_delayed_cache(item, generation)
        else:
            with self._state_lock:
                self._cancel_cache_delay_timer_locked()

    def _recent_entry_to_item_locked(self, entry):
        valid_tracks = [track for track in entry.tracks if track.is_cached()]
        if not valid_tracks:
            del self._recent_entries[entry.cache_key]
            self._save_recent_index_locked()
            self._mark_recent_dirty_locked()
            return None

        if len(valid_tracks) != len(entry.tracks):
            entry.tracks = valid_tracks
            self._save_recent_index_locked()
            self._mark_recent_dirty_locked()

        return ResolvedItem(
            kind=entry.kind,
            id=entry.id,
            title=entry.title,
            source_url=entry.source_url,
            tracks=list(valid_tracks),
        )

    def _play_recent_entry(self, item_key):
        with self._state_lock:
            entry = self._recent_entries.get(item_key)
            if entry is None:
                return
            item = self._recent_entry_to_item_locked(entry)

        if item is None:
            return

        self._start_item_playback(item, playback_mode="local")

    def on_paste_url(self, _):
        url = self._get_clipboard().strip().replace("\\", "")
        if not url or not url.startswith("http"):
            return

        def _resolve_and_play():
            item = self._resolve_url(url)
            if item is None:
                if not self.engine.is_active:
                    self._enqueue_ui_action("stopped")
                return

            playback_mode = "local" if item.is_fully_cached() else "stream"
            self._start_item_playback(item, playback_mode=playback_mode)

        threading.Thread(target=_resolve_and_play, daemon=True).start()

    def on_playpause(self, _):
        self._toggle_play_pause()

    def terminate(self):
        self._cleanup_before_quit()

    def _cleanup_before_quit(self, *_args, **_kwargs):
        # Stop main-thread timers first so no further _update_viz / _update_progress
        # runs during teardown (they fire in event-tracking mode now too).
        for timer_attr, target_attr in (
            ("_viz_timer", "_viz_timer_target"),
            ("_progress_timer", "_progress_timer_target"),
        ):
            timer = getattr(self, timer_attr, None)
            if timer is not None:
                timer.invalidate()
            setattr(self, timer_attr, None)
            setattr(self, target_attr, None)

        with self._state_lock:
            if self._cleanup_done:
                return
            self._cleanup_done = True
            self._cancel_cache_delay_timer_locked()
            self._pending_actions.clear()
        self._clear_now_playing_info()
        self._unregister_remote_commands()
        self._cache_shutdown.set()
        for _ in self._cache_workers:
            self._cache_jobs.put(None)
        for worker in self._cache_workers:
            worker.join(timeout=1)
        self.engine.close()


def _signal_handler(sig, frame):
    rumps.quit_application()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    YTBar().run()
