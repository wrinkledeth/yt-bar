import signal
import threading
from collections import deque

import AppKit
import rumps

from .audio_engine import AudioEngine
from .cache import CacheManager
from .constants import PAUSE_TITLE
from .local_media import import_local_file
from .menu import MenuController
from .models import (
    MenuAction,
    MenuActionKind,
    MenuPlaylistTrackEntry,
    MenuSnapshot,
    UICommand,
    UICommandKind,
)
from .objc_bridges import (
    install_status_item_file_drop,
    schedule_common_mode_timer,
    schedule_default_mode_timer_once,
)
from .playback import LOCAL_PLAYBACK_MODE, PlaybackController
from .recent import RecentController
from .remote_commands import RemoteCommandController
from .resolver import resolve_url
from .storage import Settings, SettingsStore
from .visualizer import grid_to_braille


class YTBar(rumps.App):
    def __init__(self):
        super().__init__("yt-bar", title=None)

        self.title = "⠆⣿⠰"
        self._idle_title = "⠆⣿⠰"
        self._state_lock = threading.RLock()

        self.engine = AudioEngine()
        self.playback = PlaybackController(lock=self._state_lock)
        self._now_playing_title = "Not Playing"
        self._now_playing_playback_mode = None
        self._progress_elapsed = None
        self._progress_duration = None
        self._pending_actions = deque()
        self._cleanup_done = False
        self._local_file_panel = None
        self._local_file_picker_timer = None
        self._local_file_picker_timer_target = None
        self._local_file_panel_activation_policy = None
        self._recent_rename_timer = None
        self._recent_rename_timer_target = None
        self._pending_recent_rename_key = None

        self.settings_store = SettingsStore()
        self.recent = RecentController()
        self.cache = CacheManager(
            is_current_stream_item_active=self._is_cache_item_still_current,
            refresh_recent_for_cache=self.recent.refresh_for_cache,
        )
        self.cache.ensure_cache_dir()
        self.cache.cleanup_partial_cache_files()
        self._load_settings()
        self.recent.load()
        self.recent.sweep_stale_entries()
        rumps.events.before_start.register(self._install_status_item_file_drop)
        rumps.events.before_quit.register(self._cleanup_before_quit)

        self.menu_controller = MenuController(
            dispatch_action=self._handle_menu_action,
            apply_layout=self._apply_menu_layout,
        )
        self._render_menu()

        self.cache.start_workers()
        self.remote = RemoteCommandController(
            engine=self.engine,
            enqueue_ui_action=self._enqueue_ui_action,
            current_track=self._current_track,
            current_track_snapshot=self._current_track_snapshot,
            skip_interval=lambda: self._skip_interval,
        )

        self._viz_timer, self._viz_timer_target = schedule_common_mode_timer(0.07, self._update_viz)
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
        return self.playback.current_track()

    def _current_track_snapshot(self):
        return self.playback.current_track_snapshot()

    def _enqueue_ui_action(self, command):
        with self._state_lock:
            self._pending_actions.append(command)

    def _apply_menu_layout(self, layout):
        self.menu.clear()
        self.menu = layout

    def _menu_snapshot(self):
        song_picker_entries = self._song_picker_entries()
        return MenuSnapshot(
            now_playing_title=self._now_playing_title,
            playback_mode=self._now_playing_playback_mode,
            progress_elapsed=self._progress_elapsed,
            progress_duration=self._progress_duration,
            active=self.engine.is_active,
            paused=self.engine.is_paused,
            has_current_track=self.playback.has_current_track(),
            compact_menu=self._compact_menu,
            skip_interval=self._skip_interval,
            recent_limit=self._recent_limit,
            song_picker_enabled=bool(song_picker_entries),
            song_picker_entries=song_picker_entries,
            recent_entries=self.recent.menu_entries(self._recent_limit),
        )

    def _render_menu(self):
        self.menu_controller.render(self._menu_snapshot())

    def _song_picker_entries(self):
        return tuple(
            MenuPlaylistTrackEntry(index=index, title=track.title)
            for index, track in enumerate(self.playback.current_playlist_tracks())
        )

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

    def _on_compact_menu_toggled(self, _=None):
        self._compact_menu = not self._compact_menu
        self._save_settings()
        self._render_menu()

    def _on_skip_changed(self, seconds):
        self._skip_interval = float(seconds)
        self._save_settings()
        self._render_menu()
        self._update_remote_skip_intervals()

    def _on_recent_limit_changed(self, value):
        self._recent_limit = int(value)
        self._save_settings()
        self._render_menu()

    def _on_recent_menu_will_open(self):
        self.recent.sweep_stale_entries()
        self._render_menu()

    def _remove_recent_entry(self, cache_key):
        if self.recent.remove(cache_key):
            self._render_menu()

    def _present_recent_rename_prompt(self, _=None):
        self._recent_rename_timer = None
        self._recent_rename_timer_target = None
        cache_key = self._pending_recent_rename_key
        self._pending_recent_rename_key = None
        if cache_key is None:
            return

        title = self.recent.display_title_for_recent(cache_key)
        if title is None:
            return

        self._activate_app_for_panel()
        try:
            alert = AppKit.NSAlert.alloc().init()
            alert.setMessageText_("Rename Recent")
            alert.setInformativeText_(
                "Set a custom label for this Recent entry. Leave blank to clear it."
            )
            alert.addButtonWithTitle_("Save")
            alert.addButtonWithTitle_("Cancel")

            rect = AppKit.NSMakeRect(0, 0, 320, 24)
            text_field = AppKit.NSTextField.alloc().initWithFrame_(rect)
            text_field.setStringValue_(title)
            alert.setAccessoryView_(text_field)

            ok_response = getattr(
                AppKit,
                "NSAlertFirstButtonReturn",
                getattr(AppKit, "NSModalResponseOK", 1000),
            )
            if alert.runModal() != ok_response:
                return

            if self.recent.rename(cache_key, text_field.stringValue()):
                self._render_menu()
        finally:
            self._restore_app_after_panel()

    def _rename_recent_entry(self, cache_key):
        if self._recent_rename_timer is not None:
            return
        self._pending_recent_rename_key = cache_key
        self._recent_rename_timer, self._recent_rename_timer_target = (
            schedule_default_mode_timer_once(0.0, self._present_recent_rename_prompt)
        )

    def _set_progress_display(self, elapsed=None, duration=None):
        if elapsed is None:
            if not self.engine.is_active:
                self._progress_elapsed = None
                self._progress_duration = None
                self._render_menu()
                return
            elapsed = self.engine.elapsed
        if duration is None:
            duration = self.engine.duration
        self._progress_elapsed = elapsed
        self._progress_duration = duration
        self._render_menu()

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
        next_index = self.playback.next_track_index()

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
        self._now_playing_title = "Not Playing"
        self._now_playing_playback_mode = None
        self._set_progress_display()
        self._clear_now_playing_info()

    def _handle_menu_action(self, action: MenuAction):
        if action.kind is MenuActionKind.PLAY_FROM_CLIPBOARD:
            self.on_paste_url(None)
        elif action.kind is MenuActionKind.PLAY_LOCAL_FILE:
            self.on_play_local_file(None)
        elif action.kind is MenuActionKind.PLAY_PAUSE:
            self.on_playpause(None)
        elif action.kind is MenuActionKind.SEEK_PERCENT and action.percent is not None:
            self._seek_to_pct(action.percent)
        elif (
            action.kind is MenuActionKind.PLAY_CURRENT_PLAYLIST_TRACK
            and action.track_index is not None
        ):
            self._play_current_playlist_track(action.track_index)
        elif action.kind is MenuActionKind.PLAY_RECENT and action.cache_key is not None:
            self._play_recent_entry(action.cache_key)
        elif action.kind is MenuActionKind.RENAME_RECENT and action.cache_key is not None:
            self._rename_recent_entry(action.cache_key)
        elif action.kind is MenuActionKind.REMOVE_RECENT and action.cache_key is not None:
            self._remove_recent_entry(action.cache_key)
        elif action.kind is MenuActionKind.TOGGLE_COMPACT_MENU:
            self._on_compact_menu_toggled()
        elif action.kind is MenuActionKind.SET_SKIP_INTERVAL and action.seconds is not None:
            self._on_skip_changed(action.seconds)
        elif action.kind is MenuActionKind.SET_RECENT_LIMIT and action.recent_limit is not None:
            self._on_recent_limit_changed(action.recent_limit)
        elif action.kind is MenuActionKind.RECENT_MENU_WILL_OPEN:
            self._on_recent_menu_will_open()
        self._render_menu()

    def _perform_ui_action(self, command):
        if command.kind is UICommandKind.PLAY:
            self._play_or_resume_current_track()
            return
        if command.kind is UICommandKind.STOPPED:
            self._handle_stopped_ui()
            return
        if command.kind is UICommandKind.PAUSE:
            self._pause_current_track()
            return
        if command.kind is UICommandKind.TOGGLE:
            self._toggle_play_pause()
            return
        if command.kind is UICommandKind.SEEK_DELTA:
            self._seek_current_track_by(command.delta_seconds)

    def _update_remote_skip_intervals(self):
        self.remote.update_skip_intervals()

    def _sync_now_playing_info(self):
        self.remote.sync_now_playing_info()

    def _clear_now_playing_info(self):
        self.remote.clear_now_playing_info()

    def _update_viz(self, _):
        pending_actions = []
        with self._state_lock:
            if self._pending_actions:
                pending_actions = list(self._pending_actions)
                self._pending_actions.clear()

        for command in pending_actions:
            self._perform_ui_action(command)

        self.recent.consume_dirty()
        self._render_menu()

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
            advance = self.playback.advance_after_track_finished()
            if advance.has_next_track:
                self._pending_actions.append(UICommand.play())
            else:
                self._pending_actions.append(UICommand.stopped())

    def _on_engine_stopped(self):
        self._enqueue_ui_action(UICommand.stopped())

    def _play_track(self, index, start_time=0, paused=False):
        track_playback = self.playback.select_track(index)
        if track_playback is None:
            return

        track = track_playback.track
        start_time = self._clamp_start_time(track.duration, start_time)
        self._now_playing_title = track.title
        self._now_playing_playback_mode = track_playback.playback_mode

        self.engine.play(
            track_playback.source,
            on_finished=self._on_track_finished,
            on_stopped=self._on_engine_stopped,
            duration=track.duration,
            is_local=track_playback.is_local,
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

    def _install_status_item_file_drop(self):
        nsapp = getattr(self, "_nsapp", None)
        status_item = getattr(nsapp, "nsstatusitem", None)
        if status_item is None:
            return

        button = status_item.button()
        if button is None:
            return

        install_status_item_file_drop(button, self._handle_dropped_local_file)

    def _handle_dropped_local_file(self, source_path):
        if not source_path:
            return
        self._import_local_file_async(source_path)

    def _configure_local_file_panel(self):
        panel = AppKit.NSOpenPanel.openPanel()
        panel.setCanChooseFiles_(True)
        panel.setCanChooseDirectories_(False)
        panel.setAllowsMultipleSelection_(False)
        panel.setResolvesAliases_(True)
        return panel

    def _activate_app_for_panel(self):
        app = AppKit.NSApplication.sharedApplication()
        if app is not None:
            self._local_file_panel_activation_policy = app.activationPolicy()
            app.setActivationPolicy_(AppKit.NSApplicationActivationPolicyRegular)
            app.activateIgnoringOtherApps_(True)

    def _restore_app_after_panel(self):
        app = AppKit.NSApplication.sharedApplication()
        policy = self._local_file_panel_activation_policy
        self._local_file_panel_activation_policy = None
        if app is not None and policy is not None:
            app.setActivationPolicy_(policy)

    def _present_local_file_picker(self, _=None):
        self._local_file_picker_timer = None
        self._local_file_picker_timer_target = None
        if self._local_file_panel is not None:
            return

        panel = self._configure_local_file_panel()
        self._local_file_panel = panel
        self._activate_app_for_panel()

        ok_response = getattr(
            AppKit,
            "NSModalResponseOK",
            getattr(AppKit, "NSFileHandlingPanelOKButton", 1),
        )

        def _handle_response(response):
            self._local_file_panel = None
            self._restore_app_after_panel()
            if response != ok_response:
                return

            url = panel.URL()
            if url is None:
                return

            source_path = url.path() or ""
            if not source_path:
                return

            self._import_local_file_async(source_path)

        panel.beginWithCompletionHandler_(_handle_response)
        panel.makeKeyAndOrderFront_(None)
        panel.orderFrontRegardless()

    def _record_item_played(self, item, *, last_played=None):
        self.recent.record_item_played(item, last_played=last_played)

    def _is_cache_item_still_current(self, item, generation):
        is_current_item = self.playback.is_current_stream_item(item, generation)
        return is_current_item and (self.engine.is_active or self.engine.is_paused)

    def _start_item_playback(self, item, *, playback_mode):
        self.engine.stop()

        with self._state_lock:
            playback_start = self.playback.start_item(item, playback_mode=playback_mode)
            self._pending_actions.append(UICommand.play())

        self._record_item_played(item)
        if playback_start.should_cache:
            self.cache.schedule_delayed_cache(item, playback_start.generation)
        else:
            self.cache.cancel_delay()

    def _play_recent_entry(self, item_key):
        item = self.recent.item_for_recent(item_key)
        if item is None:
            return

        self._start_item_playback(item, playback_mode=LOCAL_PLAYBACK_MODE)

    def _play_current_playlist_track(self, index):
        if not self.playback.current_playlist_tracks():
            return
        self._play_track(index, start_time=0, paused=False)

    def on_paste_url(self, _):
        url = self._get_clipboard().strip().replace("\\", "")
        if not url or not url.startswith("http"):
            return

        def _resolve_and_play():
            item = resolve_url(url)
            if item is None:
                if not self.engine.is_active:
                    self._enqueue_ui_action(UICommand.stopped())
                return

            playback_mode = self.playback.playback_mode_for_item(item)
            self._start_item_playback(item, playback_mode=playback_mode)

        threading.Thread(target=_resolve_and_play, daemon=True).start()

    def _import_local_file_async(self, source_path):
        def _import_and_play():
            item = import_local_file(source_path)
            if item is None:
                if not self.engine.is_active:
                    self._enqueue_ui_action(UICommand.stopped())
                return

            self._start_item_playback(item, playback_mode=LOCAL_PLAYBACK_MODE)

        threading.Thread(target=_import_and_play, daemon=True).start()

    def on_play_local_file(self, _):
        if self._local_file_picker_timer is not None or self._local_file_panel is not None:
            return

        self._local_file_picker_timer, self._local_file_picker_timer_target = (
            schedule_default_mode_timer_once(0.0, self._present_local_file_picker)
        )

    def on_playpause(self, _):
        self._toggle_play_pause()
        self._render_menu()

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
