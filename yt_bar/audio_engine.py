import queue
import threading
import time

import AVFoundation
import Foundation
import numpy as np

from .constants import (
    CHANNELS,
    GRID_H,
    GRID_W,
    INTERNAL_SAMPLE_RATE,
    PCM_BUFFER_FRAMES,
    ROUTE_CHANGE_DEBOUNCE_SECONDS,
    ROUTE_RETRY_DELAYS,
    SCHEDULE_AHEAD_FRAMES,
    SEEK_TRACE_LOGGING,
    VISUALIZER_SNAPSHOT_FRAMES,
    VISUALIZER_TAP_BUFFER_FRAMES,
    WORKER_TICK_SECONDS,
)
from .core_audio import install_default_output_listener, uninstall_default_output_listener
from .decoder import DecoderPipeline
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
        self._decoder = DecoderPipeline(self._enqueue_command, self._log_seek_trace)
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
        name, *payload = command
        handler = {
            "play": self._handle_play_command,
            "stop": self._handle_stop_command,
            "set_paused": self._handle_set_paused_command,
            "seek_current": self._handle_seek_current_command,
            "route_event": self._handle_route_event_command,
            "decoder_eof": self._handle_decoder_eof_command,
            "decoder_failed": self._handle_decoder_failed_command,
            "buffer_complete": self._handle_buffer_complete_command,
            "shutdown": self._handle_shutdown_command,
        }.get(name)

        if handler is None:
            return current_session, False
        return handler(current_session, *payload)

    def _handle_play_command(self, current_session, request):
        self._pending_retry_request = None
        if current_session is not None:
            self._discard_session(current_session, reason="replaced")
        return self._attempt_start_request(request), False

    def _handle_stop_command(self, current_session, reason):
        self._pending_retry_request = None
        if current_session is not None:
            self._discard_session(current_session, reason=reason)
        self._publish_stopped()
        return None, False

    def _handle_set_paused_command(self, current_session, target):
        if current_session is not None:
            self._set_paused(current_session, target)
        return current_session, False

    def _handle_seek_current_command(self, current_session, target):
        if current_session is None or not current_session.is_local:
            return current_session, False

        self._begin_seek_trace(current_session, target)
        request = self._resume_request(current_session, start_time=target)
        if current_session.route.rebuild_pending:
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

    def _handle_route_event_command(self, current_session, reason, session_id):
        if current_session is None or session_id not in (None, current_session.id):
            return current_session, False

        current_session.route.rebuild_pending = True
        current_session.route.rebuild_deadline = time.monotonic() + ROUTE_CHANGE_DEBOUNCE_SECONDS
        print(
            "Route change detected",
            {"reason": reason, "session_id": current_session.id},
        )
        return current_session, False

    def _handle_decoder_eof_command(self, current_session, session_id, generation):
        if self._is_current_decoder_generation(current_session, session_id, generation):
            current_session.decoder.eof = True
        return current_session, False

    def _handle_decoder_failed_command(
        self,
        current_session,
        session_id,
        generation,
        error_text,
    ):
        if self._is_current_decoder_generation(current_session, session_id, generation):
            current_session.decoder.failed = True
            current_session.decoder.error = error_text
        return current_session, False

    def _handle_buffer_complete_command(
        self,
        current_session,
        session_id,
        buffer_id,
        _callback_type,
    ):
        if current_session is not None and current_session.id == session_id:
            current_session.schedule.buffers.pop(buffer_id, None)
        return current_session, False

    def _handle_shutdown_command(self, current_session):
        self._pending_retry_request = None
        return current_session, True

    @staticmethod
    def _is_current_decoder_generation(session, session_id, generation):
        return (
            session is not None
            and session.id == session_id
            and session.decoder.generation == generation
        )

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
        session.schedule.last_elapsed_seconds = request.start_time

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
        session.seek_trace.id = self._seek_trace_counter
        session.seek_trace.started_at = time.perf_counter()
        session.seek_trace.target = max(0.0, float(target))
        session.seek_trace.first_chunk_logged = False
        session.seek_trace.first_buffer_logged = False
        session.seek_trace.player_play_logged = False
        session.seek_trace.elapsed_logged = False
        self._log_seek_trace(
            session,
            "requested",
            current_elapsed=round(session.schedule.last_elapsed_seconds, 3),
        )

    def _log_seek_trace(self, session, event, **payload):
        if not SEEK_TRACE_LOGGING or session.seek_trace.id == 0:
            return

        details = {
            "seek_id": session.seek_trace.id,
            "event": event,
            "ms": round((time.perf_counter() - session.seek_trace.started_at) * 1000, 1),
            "target": round(session.seek_trace.target, 3),
            "paused": bool(session.paused),
            "generation": session.decoder.generation,
        }
        details.update(payload)
        print("Seek trace", details)

    def _finish_seek_trace(self, session, event=None, **payload):
        if not SEEK_TRACE_LOGGING or session.seek_trace.id == 0:
            return

        if event is not None:
            self._log_seek_trace(session, event, **payload)

        session.seek_trace.id = 0
        session.seek_trace.started_at = 0.0
        session.seek_trace.target = 0.0
        session.seek_trace.first_chunk_logged = False
        session.seek_trace.first_buffer_logged = False
        session.seek_trace.player_play_logged = False
        session.seek_trace.elapsed_logged = False

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

        session.graph.engine = engine
        session.graph.player = player
        session.graph.mixer = mixer
        session.graph.format = audio_format

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
            session.graph.engine,
        )
        session.graph.notification_observer = observer

    def _install_visualizer_tap(self, session):
        tap_format = session.graph.mixer.outputFormatForBus_(0)

        def tap_block(buffer, when, sid=session.id):
            self._capture_visualizer_snapshot(sid, buffer)

        session.graph.mixer.installTapOnBus_bufferSize_format_block_(
            0,
            VISUALIZER_TAP_BUFFER_FRAMES,
            tap_format,
            tap_block,
        )
        session.graph.tap_block = tap_block

    def _start_decoder_thread(self, session):
        self._decoder.start(session)

    def _service_session(self, session):
        self._refresh_elapsed(session)

        if session.route.rebuild_pending and time.monotonic() >= session.route.rebuild_deadline:
            request = self._resume_request(
                session,
                start_time=session.schedule.last_elapsed_seconds,
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
            and session.schedule.frames_total > 0
            and not session.schedule.started_playback
        ):
            try:
                session.graph.player.play()
                session.schedule.started_playback = True
                if session.seek_trace.id != 0 and not session.seek_trace.player_play_logged:
                    session.seek_trace.player_play_logged = True
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
                    start_time=session.schedule.last_elapsed_seconds,
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
                {"error": session.decoder.error},
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
                chunk = session.decoder.queue.get_nowait()
            except queue.Empty:
                break

            if chunk is None or len(chunk) == 0:
                continue

            buffer = self._make_pcm_buffer(session.graph.format, chunk)
            buffer_id = session.schedule.next_buffer_id
            session.schedule.next_buffer_id += 1
            session.schedule.buffers[buffer_id] = buffer
            session.schedule.frames_total += len(chunk)

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
                session.graph.player.scheduleBuffer_completionCallbackType_completionHandler_(
                    buffer,
                    AVFoundation.AVAudioPlayerNodeCompletionDataPlayedBack,
                    completion_handler,
                )
                if session.seek_trace.id != 0 and not session.seek_trace.first_buffer_logged:
                    session.seek_trace.first_buffer_logged = True
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
                session.route.rebuild_pending = True
                session.route.rebuild_deadline = time.monotonic()
                break

    def _set_paused(self, session, paused):
        self._refresh_elapsed(session)
        session.paused = paused

        if paused:
            try:
                session.graph.player.pause()
            except Exception as exc:
                log_exception("AVAudioPlayerNode pause failed", exc)
            self._publish_state(active=True, starting=False, paused=True)
            return

        self._publish_state(
            active=True,
            starting=not session.schedule.started_playback,
            paused=False,
        )
        if session.schedule.started_playback and self._scheduled_ahead_frames(session) > 0:
            try:
                session.graph.player.play()
                self._publish_state(active=True, starting=False, paused=False)
            except Exception as exc:
                log_exception("AVAudioPlayerNode resume failed", exc)
                session.route.rebuild_pending = True
                session.route.rebuild_deadline = time.monotonic()

    def _scheduled_ahead_frames(self, session):
        return max(0, session.schedule.frames_total - session.schedule.last_rendered_frames)

    def _refresh_elapsed(self, session):
        elapsed = session.schedule.last_elapsed_seconds

        if session.graph.player is not None and session.schedule.started_playback:
            try:
                render_time = session.graph.player.lastRenderTime()
                if render_time is not None:
                    player_time = session.graph.player.playerTimeForNodeTime_(render_time)
                else:
                    player_time = None

                if player_time is not None and player_time.isSampleTimeValid():
                    sample_time = max(0, int(player_time.sampleTime()))
                    session.schedule.last_rendered_frames = max(
                        session.schedule.last_rendered_frames,
                        sample_time,
                    )
                    elapsed = session.base_offset_seconds + (
                        session.schedule.last_rendered_frames / INTERNAL_SAMPLE_RATE
                    )
                    if (
                        session.seek_trace.id != 0
                        and not session.seek_trace.elapsed_logged
                        and session.schedule.last_rendered_frames > 0
                    ):
                        session.seek_trace.elapsed_logged = True
                        self._finish_seek_trace(
                            session,
                            "first_elapsed_advance",
                            rendered_frames=int(session.schedule.last_rendered_frames),
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

        session.schedule.last_elapsed_seconds = elapsed
        self._publish_state(elapsed=elapsed)

    def _should_finish_naturally(self, session):
        if not session.decoder.eof or session.decoder.failed or session.route.rebuild_pending:
            return False
        if not session.decoder.queue.empty():
            return False
        if session.schedule.frames_total == 0:
            return False

        tolerance = PCM_BUFFER_FRAMES // 2
        return session.schedule.last_rendered_frames + tolerance >= session.schedule.frames_total

    def _should_stop_for_error(self, session):
        if not session.decoder.failed:
            return False
        if not session.decoder.queue.empty():
            return False
        if session.schedule.frames_total == 0:
            return True

        tolerance = PCM_BUFFER_FRAMES // 2
        return session.schedule.last_rendered_frames + tolerance >= session.schedule.frames_total

    @staticmethod
    def _resume_request(
        session,
        *,
        start_time=None,
        retry_kind=None,
        retry_attempt=0,
    ):
        if start_time is None:
            start_time = session.schedule.last_elapsed_seconds
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
        if session.graph.player is None or session.graph.engine is None:
            raise RuntimeError("Local seek requires an active player node")

        try:
            session.graph.player.stop()
        except Exception as exc:
            log_exception("AVAudioPlayerNode stop failed during seek", exc)
            raise

        self._stop_decoder(session, fast=True)
        session.base_offset_seconds = max(0.0, float(start_time))
        session.schedule.last_elapsed_seconds = session.base_offset_seconds
        session.schedule.last_rendered_frames = 0
        session.decoder.eof = False
        session.decoder.failed = False
        session.decoder.error = None
        session.schedule.buffers.clear()
        session.schedule.frames_total = 0
        session.schedule.started_playback = False
        session.route.rebuild_pending = False
        session.route.rebuild_deadline = 0.0
        self._publish_state(
            active=True,
            starting=not session.paused,
            paused=session.paused,
            is_local=True,
            duration=session.duration,
            elapsed=session.base_offset_seconds,
            reset_grid=True,
        )
        session.graph.engine.prepare()
        self._start_decoder_thread(session)
        self._log_seek_trace(
            session,
            "decoder_restarted",
            start_time=round(session.base_offset_seconds, 3),
        )

    def _stop_decoder(self, session, fast=False):
        self._decoder.stop(session, fast=fast)

    def _discard_session(
        self,
        session,
        *,
        reason,
        clear_public_state=True,
        notify_stopped=False,
    ):
        session.stop_event.set()

        if session.graph.mixer is not None:
            try:
                session.graph.mixer.removeTapOnBus_(0)
            except Exception:
                pass

        if session.graph.notification_observer is not None:
            try:
                self._notification_center.removeObserver_(session.graph.notification_observer)
            except Exception:
                pass

        if session.graph.player is not None:
            try:
                session.graph.player.stop()
            except Exception:
                pass

        if session.seek_trace.id != 0:
            self._finish_seek_trace(session, "discarded", reason=reason)

        if session.graph.engine is not None:
            try:
                session.graph.engine.stop()
            except Exception:
                pass

        self._stop_decoder(session)

        if clear_public_state:
            self._publish_stopped()
        else:
            self._publish_state(session_id=0, elapsed=session.schedule.last_elapsed_seconds)

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
