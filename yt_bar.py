import ctypes
import queue
import signal
import subprocess
import threading
import time
import traceback
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
PROGRESS_BAR_WIDTH = 18
VISUALIZER_SNAPSHOT_FRAMES = 256
VISUALIZER_TAP_BUFFER_FRAMES = 1024
DECODER_QUEUE_BUFFERS = 24

# Stereometer grid: 3 braille chars wide (6 cols) x 4 rows = 6x4 dot grid
GRID_W = 6  # dot columns (3 braille chars x 2 cols each)
GRID_H = 4  # dot rows per braille char

BRAILLE_BASE = 0x2800
DOT_BITS = [
    [0x40, 0x04, 0x02, 0x01],  # col 0 (left): bottom to top
    [0x80, 0x20, 0x10, 0x08],  # col 1 (right): bottom to top
]


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

    @property
    def duration(self):
        return self.request.duration

    @property
    def url(self):
        return self.request.url

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
        start_time=0,
        paused=False,
    ):
        request = PlayRequest(
            url=url,
            duration=duration,
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
            session_id = command[1]
            if current_session is not None and current_session.id == session_id:
                current_session.decoder_eof = True
            return current_session, False

        if name == "decoder_failed":
            session_id, error_text = command[1], command[2]
            if current_session is not None and current_session.id == session_id:
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
            duration=request.duration,
            elapsed=request.start_time,
            reset_grid=True,
        )
        return session

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
        session.decoder_thread = threading.Thread(
            target=self._decoder_loop,
            args=(session,),
            daemon=True,
        )
        session.decoder_thread.start()

    def _decoder_loop(self, session):
        ytdlp_process = None
        ffmpeg_process = None

        try:
            ytdlp_process = subprocess.Popen(
                ["yt-dlp", "-f", "bestaudio", "-o", "-", "--no-warnings", session.url],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
            ffmpeg_cmd = ["ffmpeg"]
            if session.base_offset_seconds > 0:
                ffmpeg_cmd += ["-ss", str(session.base_offset_seconds)]
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
            while not session.stop_event.is_set():
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

                while not session.stop_event.is_set():
                    try:
                        session.decoded_queue.put(chunk, timeout=0.1)
                        break
                    except queue.Full:
                        continue

            if session.stop_event.is_set():
                return

            ffmpeg_code = self._wait_process(ffmpeg_process)
            ytdlp_code = self._wait_process(ytdlp_process)
            if ffmpeg_code not in (0, None):
                raise RuntimeError(f"ffmpeg exited with status {ffmpeg_code}")
            if ytdlp_code not in (0, None):
                raise RuntimeError(f"yt-dlp exited with status {ytdlp_code}")

            self._enqueue_command("decoder_eof", session.id)
        except Exception as exc:
            if not session.stop_event.is_set():
                log_exception("Decoder error", exc)
                self._enqueue_command("decoder_failed", session.id, str(exc))
        finally:
            if session.stop_event.is_set():
                self._cleanup_process(ffmpeg_process)
                self._cleanup_process(ytdlp_process)

    def _service_session(self, session):
        self._refresh_elapsed(session)

        if session.rebuild_pending and time.monotonic() >= session.rebuild_deadline:
            request = PlayRequest(
                url=session.url,
                duration=session.duration,
                start_time=session.last_elapsed_seconds,
                paused=session.paused,
                on_finished=session.on_finished,
                on_stopped=session.on_stopped,
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
                self._publish_state(active=True, starting=False, paused=False)
            except Exception as exc:
                log_exception("AVAudioPlayerNode play failed", exc)
                request = PlayRequest(
                    url=session.url,
                    duration=session.duration,
                    start_time=session.last_elapsed_seconds,
                    paused=session.paused,
                    on_finished=session.on_finished,
                    on_stopped=session.on_stopped,
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

        if session.engine is not None:
            try:
                session.engine.stop()
            except Exception:
                pass

        self._cleanup_process(session.ffmpeg_process)
        self._cleanup_process(session.ytdlp_process)

        if session.decoder_thread is not None and session.decoder_thread.is_alive():
            session.decoder_thread.join(timeout=1)

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
    def _cleanup_process(proc):
        if proc and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                proc.kill()


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


def progress_bar(elapsed, duration, width=20):
    if duration <= 0:
        return f"{format_time(elapsed)}"
    clamped = max(0, min(elapsed, duration))
    frac = clamped / duration
    if frac <= 0:
        bar = "●" + "─" * (width - 1)
    elif frac >= 1:
        bar = "█" * width
    else:
        marker = min(width - 1, int(frac * width))
        bar = "█" * marker + "●" + "─" * (width - marker - 1)
    return f"{format_time(clamped)} ├{bar}┤ {format_time(duration)}"


class YTBar(rumps.App):
    def __init__(self):
        super().__init__("yt-bar", title=None)

        self.title = "⠆⣿⠰"
        self._idle_title = "⠆⣿⠰"

        self.engine = AudioEngine()
        self._tracks = []
        self._current_index = -1
        self._pending_ui = None

        self._now_playing = rumps.MenuItem("Not Playing")
        self._now_playing.set_callback(None)

        self._progress = rumps.MenuItem("")
        self._progress.set_callback(None)

        self._seek_menu = rumps.MenuItem("Seek")
        self._seek_items = []
        for i in range(10):
            pct = i * 10
            item = rumps.MenuItem(
                self._seek_label(i, -1),
                callback=lambda _, p=pct: self._seek_to_pct(p),
            )
            self._seek_items.append(item)
            self._seek_menu[item.title] = item

        self._playpause_item = rumps.MenuItem(
            "Play / Pause", callback=self.on_playpause
        )
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
            self._paste_item,
        ]

        self._viz_timer = rumps.Timer(self._update_viz, 0.07)
        self._viz_timer.start()

        self._progress_timer = rumps.Timer(self._update_progress, 1.0)
        self._progress_timer.start()

    @staticmethod
    def _seek_label(index, current_segment):
        pct = index * 10
        marker = "●" if index == current_segment else "○"
        return f"  {marker} {pct}%"

    def _current_track(self):
        if 0 <= self._current_index < len(self._tracks):
            return self._tracks[self._current_index]
        return None

    @staticmethod
    def _clamp_start_time(duration, start_time):
        if duration <= 0:
            return max(0.0, start_time)
        max_start = max(0.0, duration - 0.25)
        return max(0.0, min(start_time, max_start))

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
                self._progress.title = ""
                self._update_seek_markers()
                return
            elapsed = self.engine.elapsed
        if duration is None:
            duration = self.engine.duration
        self._progress.title = progress_bar(
            elapsed,
            duration,
            width=PROGRESS_BAR_WIDTH,
        )
        self._update_seek_markers(elapsed, duration)

    def _seek_to_pct(self, pct):
        track = self._current_track()
        if not track or not self.engine.is_active or self.engine.duration <= 0:
            return
        target_sec = int(self.engine.duration * pct / 100)
        self._play_track(self._current_index, start_time=target_sec)

    def _update_viz(self, _):
        pending = self._pending_ui
        if pending:
            self._pending_ui = None
            if pending == "play":
                self._play_track(self._current_index)
            elif pending == "stopped":
                self._now_playing.title = "Not Playing"
                self._set_progress_display()

        if self.engine.is_playing:
            self.title = grid_to_braille(self.engine.dot_grid)
        elif self.engine.is_active:
            self.title = "⣿⣿"
        elif self.title != self._idle_title:
            self.title = self._idle_title

    def _update_progress(self, _):
        self._set_progress_display()

    def _on_track_finished(self):
        if self._current_index + 1 < len(self._tracks):
            self._current_index += 1
            self._pending_ui = "play"
        else:
            self._pending_ui = "stopped"

    def _on_engine_stopped(self):
        self._pending_ui = "stopped"

    def _play_track(self, index, start_time=0, paused=False):
        if index < 0 or index >= len(self._tracks):
            return
        track = self._tracks[index]
        start_time = self._clamp_start_time(track.get("duration", 0), start_time)
        self._current_index = index
        self._now_playing.title = track["title"]
        self.engine.play(
            track["url"],
            on_finished=self._on_track_finished,
            on_stopped=self._on_engine_stopped,
            duration=track.get("duration", 0),
            start_time=start_time,
            paused=paused,
        )
        self._set_progress_display(
            elapsed=start_time,
            duration=track.get("duration", 0),
        )

    def _is_playlist_url(self, url):
        return "list=" in url or "/playlist" in url

    @staticmethod
    def _parse_duration(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0

    def _run_yt_dlp(self, args, timeout):
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

        return result

    def _resolve_url(self, url):
        if self._is_playlist_url(url):
            result = self._run_yt_dlp(
                [
                    "yt-dlp",
                    "--flat-playlist",
                    "--print",
                    (
                        f"%(webpage_url)s{YTDLP_FIELD_SEP}"
                        f"%(title)s{YTDLP_FIELD_SEP}%(duration)s"
                    ),
                    "--no-warnings",
                    url,
                ],
                timeout=60,
            )
            if result:
                tracks = []
                for line in result.stdout.splitlines():
                    if not line.strip():
                        continue
                    parts = line.split(YTDLP_FIELD_SEP)
                    track_url = parts[0].strip() if parts else url
                    if not track_url.startswith("http"):
                        track_url = url
                    title = (
                        parts[1].strip()
                        if len(parts) > 1 and parts[1].strip()
                        else "Unknown"
                    )
                    duration = self._parse_duration(parts[2] if len(parts) > 2 else 0)
                    tracks.append(
                        {
                            "url": track_url,
                            "title": title,
                            "duration": duration,
                        }
                    )
                if tracks:
                    return tracks
                print(f"yt-dlp returned no playlist entries for {url}")

        result = self._run_yt_dlp(
            [
                "yt-dlp",
                "--print",
                f"%(title)s{YTDLP_FIELD_SEP}%(duration)s",
                "--no-warnings",
                "--no-download",
                "--no-playlist",
                url,
            ],
            timeout=15,
        )
        if not result or not result.stdout.strip():
            return []

        parts = result.stdout.strip().split(YTDLP_FIELD_SEP)
        title = parts[0].strip() if parts and parts[0].strip() else "Unknown"
        duration = self._parse_duration(parts[1] if len(parts) > 1 else 0)
        return [{"url": url, "title": title, "duration": duration}]

    def _get_clipboard(self):
        pb = AppKit.NSPasteboard.generalPasteboard()
        return pb.stringForType_(AppKit.NSStringPboardType) or ""

    def on_paste_url(self, _):
        url = self._get_clipboard().strip().replace("\\", "")
        if not url or not url.startswith("http"):
            return

        def _resolve_and_play():
            tracks = self._resolve_url(url)
            if not tracks:
                if not self.engine.is_active:
                    self._pending_ui = "stopped"
                return
            self.engine.stop()
            self._tracks = tracks
            self._current_index = 0
            self._pending_ui = "play"

        threading.Thread(target=_resolve_and_play, daemon=True).start()

    def on_playpause(self, _):
        if self.engine.is_active:
            self.engine.toggle_pause()
        elif self._tracks and self._current_index >= 0:
            self._play_track(self._current_index)

    def terminate(self):
        self.engine.close()
        super().terminate()


def _signal_handler(sig, frame):
    rumps.quit_application()


if __name__ == "__main__":
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    YTBar().run()
