"""Background cache worker ownership.

Locking contract:
- CacheManager may use its private lock for timer, worker, and scheduled-id state.
- CacheManager never acquires the app's state lock.
- Callers must not call CacheManager methods while holding the app's state lock.
- CacheManager never invokes app boundary callbacks while holding its private lock.
- App callbacks are responsible for their own synchronization and for routing UI
  changes back through the main-thread handoff.
"""

import os
import queue
import subprocess
import threading

from .constants import (
    CACHE_DELAY_SECONDS,
    CACHE_WORKER_COUNT,
    PARTIAL_CACHE_SUFFIX,
    SONGS_DIR,
)
from .models import CacheJob


class CacheManager:
    def __init__(
        self,
        *,
        is_current_stream_item_active,
        refresh_recent_for_cache,
        songs_dir=SONGS_DIR,
        delay_seconds=CACHE_DELAY_SECONDS,
        worker_count=CACHE_WORKER_COUNT,
    ):
        self._is_current_stream_item_active = is_current_stream_item_active
        self._refresh_recent_for_cache = refresh_recent_for_cache
        self._songs_dir = songs_dir
        self._delay_seconds = delay_seconds
        self._worker_count = worker_count
        self._lock = threading.RLock()
        self._jobs: queue.Queue = queue.Queue()
        self._shutdown = threading.Event()
        self._workers = []
        self._delay_timer = None
        self._scheduled_track_ids: set[str] = set()

    def ensure_cache_dir(self):
        os.makedirs(self._songs_dir, exist_ok=True)

    def cleanup_partial_cache_files(self):
        for root, _dirs, names in os.walk(self._songs_dir):
            for name in names:
                if not name.endswith(PARTIAL_CACHE_SUFFIX):
                    continue
                path = os.path.join(root, name)
                try:
                    os.remove(path)
                except OSError:
                    pass

    def start_workers(self):
        for _ in range(self._worker_count):
            worker = threading.Thread(target=self._worker_loop, daemon=True)
            worker.start()
            self._workers.append(worker)

    def schedule_delayed_cache(self, item, generation):
        self.cancel_delay()
        timer = threading.Timer(
            self._delay_seconds,
            lambda: self._start_cache_if_still_current(item, generation),
        )
        timer.daemon = True
        with self._lock:
            self._delay_timer = timer
        timer.start()

    def cancel_delay(self):
        with self._lock:
            timer = self._delay_timer
            self._delay_timer = None
        if timer is not None:
            timer.cancel()

    def shutdown(self):
        self.cancel_delay()
        self._shutdown.set()
        workers = list(self._workers)
        for _ in workers:
            self._jobs.put(None)
        for worker in workers:
            worker.join(timeout=1)

    def _start_cache_if_still_current(self, item, generation):
        if not self._is_current_stream_item_active(item, generation):
            return
        self.enqueue_cache_jobs_for_item(item)

    def enqueue_cache_jobs_for_item(self, item):
        self._refresh_recent_for_cache(item)

        for track in item.tracks:
            if track.is_cached() or not track.source_url:
                continue
            with self._lock:
                if track.id in self._scheduled_track_ids:
                    continue
                self._scheduled_track_ids.add(track.id)
            self._jobs.put(CacheJob(item=item, track=track))

    def _download_track_cache(self, track):
        final_path = track.absolute_local_path
        partial_path = track.partial_local_path
        output_template = f"{os.path.splitext(partial_path)[0]}.%(ext)s"

        if os.path.exists(final_path):
            return True

        os.makedirs(os.path.dirname(final_path), exist_ok=True)

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

    def _worker_loop(self):
        while not self._shutdown.is_set():
            try:
                job = self._jobs.get(timeout=0.2)
            except queue.Empty:
                continue

            if job is None:
                self._jobs.task_done()
                break

            try:
                if self._download_track_cache(job.track):
                    self._refresh_recent_for_cache(job.item)
            finally:
                with self._lock:
                    self._scheduled_track_ids.discard(job.track.id)
                self._jobs.task_done()
