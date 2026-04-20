import signal
import threading
import time
from collections import deque

import AppKit
import rumps

from .audio_engine import AudioEngine
from .cache import CacheManager
from .constants import PAUSE_TITLE
from .menu import MenuController
from .models import RecentItem, ResolvedItem
from .objc_bridges import schedule_common_mode_timer
from .remote_commands import RemoteCommandController
from .resolver import resolve_url
from .storage import RecentStore, Settings, SettingsStore
from .visualizer import grid_to_braille


class YTBar(rumps.App):
    def __init__(self):
        super().__init__("yt-bar", title=None)

        self.title = "⠆⣿⠰"
        self._idle_title = "⠆⣿⠰"
        self._state_lock = threading.RLock()

        self.engine = AudioEngine()
        self._tracks = []
        self._current_index = -1
        self._current_item: ResolvedItem | None = None
        self._current_playback_mode = "stream"
        self._current_item_generation = 0
        self._pending_actions = deque()
        self._recent_dirty = False
        self._recent_entries: dict[str, RecentItem] = {}
        self._item_last_played: dict[str, float] = {}
        self._cleanup_done = False

        self.settings_store = SettingsStore()
        self.recent_store = RecentStore()
        self.cache = CacheManager(
            is_current_stream_item_active=self._is_cache_item_still_current,
            refresh_recent_for_cache=self._refresh_recent_for_cache,
        )
        self.cache.ensure_cache_dir()
        self.cache.cleanup_partial_cache_files()
        self._load_settings()
        self._load_recent_index()
        self._sweep_stale_recent_entries()
        rumps.events.before_quit.register(self._cleanup_before_quit)

        self.menu_controller = MenuController(self)
        self.menu_controller.apply_settings_check_marks()
        self.menu_controller.apply_layout()
        self.menu_controller.rebuild_recent_menu()
        self.menu_controller.install_recent_menu_delegate()
        self.menu_controller.refresh_playback_items()

        self.cache.start_workers()
        self.remote = RemoteCommandController(
            engine=self.engine,
            enqueue_ui_action=self._enqueue_ui_action,
            current_track=self._current_track,
            current_track_snapshot=self._current_track_snapshot,
            skip_interval=lambda: self._skip_interval,
        )

        self._viz_timer, self._viz_timer_target = schedule_common_mode_timer(
            0.07, self._update_viz
        )
        self._progress_timer, self._progress_timer_target = schedule_common_mode_timer(
            1.0, self._update_progress
        )

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

    def _load_settings(self):
        settings = self.settings_store.load()
        self._skip_interval = settings.skip_interval_seconds
        self._recent_limit = settings.recent_menu_limit
        self._compact_menu = settings.compact_menu

    def _save_settings(self):
        self.settings_store.save(
            Settings(
                skip_interval_seconds=self._skip_interval,
                recent_menu_limit=self._recent_limit,
                compact_menu=self._compact_menu,
            )
        )

    def _load_recent_index(self):
        with self._state_lock:
            self._recent_entries = self.recent_store.load()

    def _save_recent_index_locked(self):
        self.recent_store.save(self._recent_entries)

    def _mark_recent_dirty_locked(self):
        self._recent_dirty = True

    def _sweep_stale_recent_entries_locked(self):
        changed = self.recent_store.sweep_stale_entries(self._recent_entries)
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
        return entries[: self._recent_limit]

    def _on_compact_menu_toggled(self, _):
        self._compact_menu = not self._compact_menu
        self._save_settings()
        self.menu_controller.apply_settings_check_marks()
        self.menu_controller.apply_layout()
        self.menu_controller.install_recent_menu_delegate()
        self.menu_controller.refresh_playback_items()

    def _on_skip_changed(self, seconds):
        self._skip_interval = float(seconds)
        self._save_settings()
        self.menu_controller.apply_settings_check_marks()
        self._update_remote_skip_intervals()

    def _on_recent_limit_changed(self, value):
        self._recent_limit = int(value)
        self._save_settings()
        self.menu_controller.apply_settings_check_marks()
        self.menu_controller.rebuild_recent_menu()

    def _on_recent_menu_will_open(self):
        self._sweep_stale_recent_entries()
        self.menu_controller.rebuild_recent_menu()

    def _remove_recent_entry(self, cache_key):
        changed = False
        with self._state_lock:
            if cache_key in self._recent_entries:
                del self._recent_entries[cache_key]
                self._save_recent_index_locked()
                changed = True
        if changed:
            self.menu_controller.rebuild_recent_menu()

    def _set_progress_display(self, elapsed=None, duration=None):
        self.menu_controller.set_progress_display(elapsed=elapsed, duration=duration)

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

    def _skip_forward_to_next_track(self):
        paused = self.engine.is_paused
        with self._state_lock:
            next_index = self._current_index + 1
            if next_index >= len(self._tracks):
                next_index = None

        if next_index is None:
            self.engine.stop()
            self._handle_stopped_ui()
            return True

        self._play_track(next_index, start_time=0, paused=paused)
        return True

    def _seek_current_track_by(self, delta_seconds):
        duration = self.engine.duration
        if duration <= 0:
            return False

        target = self.engine.elapsed + delta_seconds
        if delta_seconds > 0 and target >= duration:
            return self._skip_forward_to_next_track()

        return self._seek_current_track_to(target)

    def _handle_stopped_ui(self):
        if self.engine.is_active:
            return
        self.menu_controller.set_not_playing()
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

    def _update_remote_skip_intervals(self):
        self.remote.update_skip_intervals()

    def _sync_now_playing_info(self):
        self.remote.sync_now_playing_info()

    def _clear_now_playing_info(self):
        self.remote.clear_now_playing_info()

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
            self.menu_controller.rebuild_recent_menu()

        for action in pending_actions:
            self._perform_ui_action(*action)

        self.menu_controller.refresh_playback_items()

        if self.engine.is_playing:
            self.title = grid_to_braille(self.engine.dot_grid)
        elif self.engine.is_active:
            self.title = PAUSE_TITLE
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
        self.menu_controller.set_now_playing(track.title, playback_mode)

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

    def _is_cache_item_still_current(self, item, generation):
        with self._state_lock:
            is_current_item = (
                generation == self._current_item_generation
                and self._current_item is not None
                and self._current_item.cache_key == item.cache_key
                and self._current_playback_mode == "stream"
            )
        return is_current_item and (self.engine.is_active or self.engine.is_paused)

    def _refresh_recent_for_cache(self, item):
        with self._state_lock:
            self._refresh_recent_from_item_locked(
                item,
                last_played=self._latest_last_played_locked(item.cache_key),
                remove_if_empty=False,
            )

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
            self.cache.schedule_delayed_cache(item, generation)
        else:
            self.cache.cancel_delay()

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
            item = resolve_url(url)
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
            self._pending_actions.clear()

        self.remote.close()
        self.cache.shutdown()
        self.engine.close()


def _signal_handler(sig, frame):
    rumps.quit_application()


def main():
    signal.signal(signal.SIGINT, _signal_handler)
    signal.signal(signal.SIGTERM, _signal_handler)
    YTBar().run()


if __name__ == "__main__":
    main()
