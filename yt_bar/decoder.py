import queue
import subprocess
import threading
from collections.abc import Callable

import numpy as np

from .constants import (
    CHANNELS,
    DECODER_QUEUE_BUFFERS,
    INTERNAL_SAMPLE_RATE,
    PCM_BUFFER_FRAMES,
    PCM_BYTES_PER_FRAME,
)
from .utils import log_exception


def build_ytdlp_command(url):
    return [
        "yt-dlp",
        "-f",
        "bestaudio",
        "-o",
        "-",
        "--no-warnings",
        url,
    ]


def build_ffmpeg_command(url, *, is_local, start_time=0.0):
    command = ["ffmpeg"]
    if start_time > 0:
        command += ["-ss", str(start_time)]

    input_url = url if is_local else "pipe:0"
    command += [
        "-i",
        input_url,
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
    return command


class DecoderPipeline:
    def __init__(
        self,
        enqueue_command: Callable[..., None],
        log_seek_trace: Callable[..., None],
    ):
        self._enqueue_command = enqueue_command
        self._log_seek_trace = log_seek_trace

    def start(self, session):
        session.decoder.generation += 1
        generation = session.decoder.generation
        decoder_stop_event = threading.Event()
        session.decoder.stop_event = decoder_stop_event
        session.decoder.queue = queue.Queue(maxsize=DECODER_QUEUE_BUFFERS)
        session.decoder.thread = threading.Thread(
            target=self._decode_loop,
            args=(session, generation, decoder_stop_event, session.decoder.queue),
            daemon=True,
        )
        session.decoder.thread.start()

    def stop(self, session, fast=False):
        if session.decoder.stop_event is not None:
            session.decoder.stop_event.set()

        cleanup_timeout = 0.1 if fast else 2.0
        self._cleanup_process(
            session.decoder.ffmpeg_process,
            timeout=cleanup_timeout,
            force_kill=fast,
        )
        self._cleanup_process(
            session.decoder.ytdlp_process,
            timeout=cleanup_timeout,
            force_kill=fast,
        )

        if session.decoder.thread is not None and session.decoder.thread.is_alive():
            session.decoder.thread.join(timeout=0.25 if fast else 1.0)

        session.decoder.thread = None
        session.decoder.stop_event = None
        session.decoder.ffmpeg_process = None
        session.decoder.ytdlp_process = None

    def _decode_loop(self, session, generation, decoder_stop_event, decoded_queue):
        ytdlp_process = None
        ffmpeg_process = None

        try:
            if session.stop_event.is_set() or decoder_stop_event.is_set():
                return

            ffmpeg_cmd = build_ffmpeg_command(
                session.url,
                is_local=session.is_local,
                start_time=session.base_offset_seconds,
            )
            if session.is_local:
                ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
            else:
                ytdlp_process = subprocess.Popen(
                    build_ytdlp_command(session.url),
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                ffmpeg_process = subprocess.Popen(
                    ffmpeg_cmd,
                    stdin=ytdlp_process.stdout,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.DEVNULL,
                )
                if ytdlp_process.stdout is not None:
                    ytdlp_process.stdout.close()

            session.decoder.ytdlp_process = ytdlp_process
            session.decoder.ffmpeg_process = ffmpeg_process

            bytes_per_chunk = PCM_BUFFER_FRAMES * PCM_BYTES_PER_FRAME
            while not session.stop_event.is_set() and not decoder_stop_event.is_set():
                data = ffmpeg_process.stdout.read(bytes_per_chunk)
                if not data:
                    break

                usable = len(data) - (len(data) % PCM_BYTES_PER_FRAME)
                if usable <= 0:
                    continue

                chunk = np.frombuffer(data[:usable], dtype=np.float32).reshape(-1, CHANNELS).copy()
                self._log_first_pcm_chunk(session, generation, chunk)

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
            if session.decoder.generation == generation:
                session.decoder.ffmpeg_process = None
                session.decoder.ytdlp_process = None

    def _log_first_pcm_chunk(self, session, generation, chunk):
        if (
            session.seek_trace.id != 0
            and session.decoder.generation == generation
            and not session.seek_trace.first_chunk_logged
        ):
            session.seek_trace.first_chunk_logged = True
            self._log_seek_trace(
                session,
                "first_pcm_chunk",
                chunk_frames=int(len(chunk)),
            )

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
