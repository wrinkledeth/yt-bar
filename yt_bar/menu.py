import AppKit
import rumps

from .constants import (
    PROGRESS_BAR_WIDTH,
    RECENT_SIZE_PRESETS,
    SKIP_INTERVAL_PRESETS,
)
from .models import MenuAction, MenuSnapshot
from .objc_bridges import RecentMenuObserver
from .utils import _set_header_title, progress_bar, truncate_title


class MenuController:
    def __init__(self, *, dispatch_action, apply_layout):
        self._dispatch_action = dispatch_action
        self._apply_layout = apply_layout
        self._recent_menu_observer = None
        self._last_layout_visibility = None
        self._last_recent_entries = None
        self._last_song_picker_enabled = None
        self._last_song_picker_entries = None
        self._last_now_playing = None
        self._last_progress_text = None
        self._last_seek_segment = None

        self.now_playing = rumps.MenuItem("Not Playing")
        self.now_playing.set_callback(lambda *_: None)

        self.progress = rumps.MenuItem("")
        self.progress.set_callback(lambda *_: None)

        self.seek_menu = rumps.MenuItem("Seek")
        self.seek_items = []
        for i in range(10):
            pct = i * 10
            item = rumps.MenuItem(
                self.seek_label(i, -1),
                callback=lambda _, p=pct: self._dispatch(MenuAction.seek_percent(p)),
            )
            self.seek_items.append(item)
            self.seek_menu[f"seek_{pct}"] = item

        self._playpause_callback = lambda _: self._dispatch(MenuAction.play_pause())
        self.playpause_item = rumps.MenuItem("Play / Pause", callback=self._playpause_callback)
        self.song_picker_menu = rumps.MenuItem("Songs")
        self.recent_menu = rumps.MenuItem("Recent")
        self.settings_menu = rumps.MenuItem("Settings")
        self.show_play_pause_item = rumps.MenuItem(
            "Show Play / Pause",
            callback=lambda _: self._dispatch(MenuAction.toggle_show_play_pause()),
        )
        self.show_seek_item = rumps.MenuItem(
            "Show Seek",
            callback=lambda _: self._dispatch(MenuAction.toggle_show_seek()),
        )
        self.show_songs_item = rumps.MenuItem(
            "Show Songs",
            callback=lambda _: self._dispatch(MenuAction.toggle_show_songs()),
        )
        self.skip_menu = rumps.MenuItem("Skip Interval")
        self.skip_items: dict[float, rumps.MenuItem] = {}
        for seconds in SKIP_INTERVAL_PRESETS:
            label = f"{int(seconds)}s"
            item = rumps.MenuItem(
                label,
                callback=lambda _, s=seconds: self._dispatch(MenuAction.set_skip_interval(s)),
            )
            self.skip_items[seconds] = item
            self.skip_menu[label] = item

        self.recent_size_menu = rumps.MenuItem("Recent List Size")
        self.recent_size_items: dict[int, rumps.MenuItem] = {}
        for value in RECENT_SIZE_PRESETS:
            label = str(value)
            item = rumps.MenuItem(
                label,
                callback=lambda _, v=value: self._dispatch(MenuAction.set_recent_limit(v)),
            )
            self.recent_size_items[value] = item
            self.recent_size_menu[label] = item

        self.settings_menu["Show Play / Pause"] = self.show_play_pause_item
        self.settings_menu["Show Seek"] = self.show_seek_item
        self.settings_menu["Show Songs"] = self.show_songs_item
        self.settings_menu["settings_separator"] = None
        self.settings_menu["Skip Interval"] = self.skip_menu
        self.settings_menu["Recent List Size"] = self.recent_size_menu
        self.paste_item = rumps.MenuItem(
            "Play from Clipboard",
            callback=lambda _: self._dispatch(MenuAction.play_from_clipboard()),
        )
        self.local_file_item = rumps.MenuItem(
            "Play Local File...",
            callback=lambda _: self._dispatch(MenuAction.play_local_file()),
        )

    def _dispatch(self, action):
        self._dispatch_action(action)

    @staticmethod
    def seek_label(index, current_segment):
        pct = index * 10
        marker = "●" if index == current_segment else "○"
        return f"  {marker} {pct}%"

    @staticmethod
    def set_menu_item_enabled(menu_item, enabled):
        menu_item._menuitem.setEnabled_(bool(enabled))

    def render(self, snapshot: MenuSnapshot):
        self.apply_layout(snapshot)
        self.apply_settings_check_marks(snapshot)
        self.refresh_playback_items(snapshot)
        self.rebuild_song_picker_menu(snapshot)
        self.rebuild_recent_menu(snapshot)
        self.set_now_playing(snapshot)
        self.set_progress_display(snapshot)

    def apply_layout(self, snapshot: MenuSnapshot):
        layout_visibility = (
            snapshot.show_play_pause,
            snapshot.show_seek,
            snapshot.show_songs,
        )
        if self._last_layout_visibility == layout_visibility:
            return
        layout = [
            self.now_playing,
            self.progress,
            None,
            self.paste_item,
            self.local_file_item,
            self.recent_menu,
        ]
        transport_items = []
        if snapshot.show_play_pause:
            transport_items.append(self.playpause_item)
        if snapshot.show_seek:
            transport_items.append(self.seek_menu)
        if snapshot.show_songs:
            transport_items.append(self.song_picker_menu)
        layout.append(None)
        if transport_items:
            layout.extend(transport_items)
            layout.append(None)
        layout.append(self.settings_menu)
        self._apply_layout(layout)
        self._last_layout_visibility = layout_visibility
        self.install_recent_menu_delegate()

    def refresh_playback_items(self, snapshot: MenuSnapshot):
        if snapshot.active:
            transport_title = "Resume" if snapshot.paused else "Pause"
            transport_enabled = True
        else:
            transport_title = "Play"
            transport_enabled = snapshot.has_current_track

        if self.playpause_item.title != transport_title:
            self.playpause_item.title = transport_title

        if transport_enabled and self.playpause_item.callback is None:
            self.playpause_item.set_callback(self._playpause_callback)
        elif not transport_enabled and self.playpause_item.callback is not None:
            self.playpause_item.set_callback(None)

        if self.playpause_item._menuitem.isEnabled() != transport_enabled:
            self.set_menu_item_enabled(self.playpause_item, transport_enabled)

        seek_enabled = snapshot.active and (snapshot.progress_duration or 0) > 0
        if self.seek_menu._menuitem.isEnabled() != seek_enabled:
            self.set_menu_item_enabled(self.seek_menu, seek_enabled)

        if self.song_picker_menu._menuitem.isEnabled() != snapshot.song_picker_enabled:
            self.set_menu_item_enabled(self.song_picker_menu, snapshot.song_picker_enabled)

    def install_recent_menu_delegate(self):
        if self.recent_menu._menu is None:
            return
        observer = RecentMenuObserver.alloc().initWithCallback_(
            lambda: self._dispatch(MenuAction.recent_menu_will_open())
        )
        self.recent_menu._menu.setDelegate_(observer)
        self._recent_menu_observer = observer

    def apply_settings_check_marks(self, snapshot: MenuSnapshot):
        self.show_play_pause_item.state = 1 if snapshot.show_play_pause else 0
        self.show_seek_item.state = 1 if snapshot.show_seek else 0
        self.show_songs_item.state = 1 if snapshot.show_songs else 0
        for seconds, item in self.skip_items.items():
            item.state = 1 if seconds == snapshot.skip_interval else 0
        for value, item in self.recent_size_items.items():
            item.state = 1 if value == snapshot.recent_limit else 0

    @staticmethod
    def modifier_flag(name, fallback):
        return getattr(AppKit, name, fallback)

    @classmethod
    def mark_alternate(cls, menu_item, modifier_mask):
        menu_item._menuitem.setAlternate_(True)
        menu_item._menuitem.setKeyEquivalentModifierMask_(modifier_mask)

    def rebuild_recent_menu(self, snapshot: MenuSnapshot):
        entries = snapshot.recent_entries
        if self._last_recent_entries == entries:
            return
        if self.recent_menu._menu is not None:
            self.recent_menu.clear()

        if not entries:
            placeholder = rumps.MenuItem("No recent items")
            placeholder.set_callback(None)
            self.recent_menu["recent_empty"] = placeholder
            self.install_recent_menu_delegate()
            self._last_recent_entries = entries
            return

        for index, entry in enumerate(entries):
            title = truncate_title(entry.title)
            option_mask = self.modifier_flag(
                "NSEventModifierFlagOption",
                AppKit.NSAlternateKeyMask,
            )
            shift_mask = self.modifier_flag(
                "NSEventModifierFlagShift",
                AppKit.NSShiftKeyMask,
            )
            play_item = rumps.MenuItem(
                title,
                callback=lambda _, key=entry.cache_key: self._dispatch(MenuAction.play_recent(key)),
            )
            remove_item = rumps.MenuItem(
                f"✕ {title}",
                callback=lambda _, key=entry.cache_key: self._dispatch(
                    MenuAction.remove_recent(key)
                ),
            )
            rename_item = rumps.MenuItem(
                "Rename…",
                callback=lambda _, key=entry.cache_key: self._dispatch(
                    MenuAction.rename_recent(key)
                ),
            )
            self.mark_alternate(remove_item, option_mask)
            self.mark_alternate(rename_item, option_mask | shift_mask)
            self.recent_menu[f"recent_play_{index}"] = play_item
            self.recent_menu[f"recent_remove_{index}"] = remove_item
            self.recent_menu[f"recent_rename_{index}"] = rename_item

        self.install_recent_menu_delegate()
        self._last_recent_entries = entries

    def rebuild_song_picker_menu(self, snapshot: MenuSnapshot):
        entries = snapshot.song_picker_entries
        enabled = snapshot.song_picker_enabled
        if self._last_song_picker_entries == entries and self._last_song_picker_enabled == enabled:
            return
        if self.song_picker_menu._menu is not None:
            self.song_picker_menu.clear()

        if not entries:
            placeholder = rumps.MenuItem("No songs available")
            placeholder.set_callback(None)
            self.song_picker_menu["song_picker_empty"] = placeholder
            self._last_song_picker_entries = entries
            self._last_song_picker_enabled = enabled
            return

        for entry in entries:
            track_item = rumps.MenuItem(
                truncate_title(entry.title),
                callback=lambda _, index=entry.index: self._dispatch(
                    MenuAction.play_current_playlist_track(index)
                ),
            )
            self.song_picker_menu[f"song_picker_{entry.index}"] = track_item

        self._last_song_picker_entries = entries
        self._last_song_picker_enabled = enabled

    def update_seek_markers(self, current_segment=None):
        if self._last_seek_segment == current_segment:
            return
        for index, item in enumerate(self.seek_items):
            item.title = self.seek_label(index, current_segment)
        self._last_seek_segment = current_segment

    def set_progress_display(self, snapshot: MenuSnapshot):
        elapsed = snapshot.progress_elapsed
        duration = snapshot.progress_duration
        current_segment = None
        if elapsed is None:
            progress_text = ""
        else:
            progress_text = progress_bar(elapsed, duration, width=PROGRESS_BAR_WIDTH)
            if duration and duration > 0:
                frac = max(0, min(elapsed, duration)) / duration
                current_segment = min(9, int(frac * len(self.seek_items)))

        if self._last_progress_text != progress_text:
            _set_header_title(self.progress, progress_text)
            self._last_progress_text = progress_text
        self.update_seek_markers(current_segment)

    def set_now_playing(self, snapshot: MenuSnapshot):
        now_playing = (snapshot.now_playing_title, snapshot.playback_mode)
        if self._last_now_playing == now_playing:
            return
        if snapshot.playback_mode is None:
            _set_header_title(self.now_playing, snapshot.now_playing_title)
        else:
            badge = "◌" if snapshot.playback_mode == "stream" else "●"
            _set_header_title(self.now_playing, snapshot.now_playing_title, trailing=badge)
        self._last_now_playing = now_playing
