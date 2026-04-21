import queue
import subprocess
import threading
import time

import AVFoundation
import Foundation
import numpy as np

from .constants import (
    CHANNELS,
    DECODER_QUEUE_BUFFERS,
    GRID_H,
    GRID_W,
    INTERNAL_SAMPLE_RATE,
    PCM_BUFFER_FRAMES,
    PCM_BYTES_PER_FRAME,
    ROUTE_CHANGE_DEBOUNCE_SECONDS,
    ROUTE_RETRY_DELAYS,
    SCHEDULE_AHEAD_FRAMES,
    SEEK_TRACE_LOGGING,
    VISUALIZER_SNAPSHOT_FRAMES,
    VISUALIZER_TAP_BUFFER_FRAMES,
    WORKER_TICK_SECONDS,
)
from .core_audio import install_default_output_listener, uninstall_default_output_listener
from .models import PlaybackSession, PlayRequest
from .objc_bridges import EngineConfigurationObserver
from .utils import log_exception


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
        self._output_listener = None
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
            current_session.rebuild_deadline = time.monotonic() + ROUTE_CHANGE_DEBOUNCE_SECONDS
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

                chunk = np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, CHANNELS).copy()
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

        if (
            not session.paused
            and session.scheduled_frames_total > 0
            and not session.started_playback
        ):
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
        self._output_listener = install_default_output_listener(
            lambda: self._enqueue_command("route_event", "default_output", None)
        )

    def _remove_default_output_listener(self):
        uninstall_default_output_listener(self._output_listener)
        self._output_listener = None

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
