import AppKit
import rumps

from .constants import (
    PROGRESS_BAR_WIDTH,
    RECENT_SIZE_PRESETS,
    SKIP_INTERVAL_PRESETS,
)
from .objc_bridges import RecentMenuObserver
from .utils import _set_header_title, progress_bar, truncate_title


class MenuController:
    def __init__(self, app):
        self.app = app
        self._recent_menu_observer = None

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
                callback=lambda _, p=pct: self.app._seek_to_pct(p),
            )
            self.seek_items.append(item)
            self.seek_menu[f"seek_{pct}"] = item

        self.playpause_item = rumps.MenuItem("Play / Pause", callback=self.app.on_playpause)
        self.recent_menu = rumps.MenuItem("Recent")
        self.settings_menu = rumps.MenuItem("Settings")
        self.compact_menu_item = rumps.MenuItem(
            "Compact Menu", callback=self.app._on_compact_menu_toggled
        )
        self.skip_menu = rumps.MenuItem("Skip Interval")
        self.skip_items: dict[float, rumps.MenuItem] = {}
        for seconds in SKIP_INTERVAL_PRESETS:
            label = f"{int(seconds)}s"
            item = rumps.MenuItem(
                label,
                callback=lambda _, s=seconds: self.app._on_skip_changed(s),
            )
            self.skip_items[seconds] = item
            self.skip_menu[label] = item

        self.recent_size_menu = rumps.MenuItem("Recent List Size")
        self.recent_size_items: dict[int, rumps.MenuItem] = {}
        for value in RECENT_SIZE_PRESETS:
            label = str(value)
            item = rumps.MenuItem(
                label,
                callback=lambda _, v=value: self.app._on_recent_limit_changed(v),
            )
            self.recent_size_items[value] = item
            self.recent_size_menu[label] = item

        self.settings_menu["Compact Menu"] = self.compact_menu_item
        self.settings_menu["Skip Interval"] = self.skip_menu
        self.settings_menu["Recent List Size"] = self.recent_size_menu
        self.paste_item = rumps.MenuItem("Play from Clipboard", callback=self.app.on_paste_url)

    @staticmethod
    def seek_label(index, current_segment):
        pct = index * 10
        marker = "●" if index == current_segment else "○"
        return f"  {marker} {pct}%"

    @staticmethod
    def set_menu_item_enabled(menu_item, enabled):
        menu_item._menuitem.setEnabled_(bool(enabled))

    def apply_layout(self):
        layout = [
            self.now_playing,
            self.progress,
            None,
            self.paste_item,
            self.recent_menu,
            None,
        ]
        if not self.app._compact_menu:
            layout.extend(
                [
                    self.playpause_item,
                    self.seek_menu,
                    None,
                ]
            )
        layout.append(self.settings_menu)
        self.app.menu.clear()
        self.app.menu = layout

    def refresh_playback_items(self):
        current_index, track = self.app._current_track_snapshot()

        if self.app.engine.is_active:
            transport_title = "Resume" if self.app.engine.is_paused else "Pause"
            transport_enabled = True
        else:
            transport_title = "Play"
            transport_enabled = track is not None and current_index >= 0

        if self.playpause_item.title != transport_title:
            self.playpause_item.title = transport_title

        if transport_enabled and self.playpause_item.callback is None:
            self.playpause_item.set_callback(self.app.on_playpause)
        elif not transport_enabled and self.playpause_item.callback is not None:
            self.playpause_item.set_callback(None)

        if self.playpause_item._menuitem.isEnabled() != transport_enabled:
            self.set_menu_item_enabled(self.playpause_item, transport_enabled)

        seek_enabled = self.app.engine.is_active and self.app.engine.duration > 0
        if self.seek_menu._menuitem.isEnabled() != seek_enabled:
            self.set_menu_item_enabled(self.seek_menu, seek_enabled)

    def install_recent_menu_delegate(self):
        if self.recent_menu._menu is None:
            return
        observer = RecentMenuObserver.alloc().initWithCallback_(self.app._on_recent_menu_will_open)
        self.recent_menu._menu.setDelegate_(observer)
        self._recent_menu_observer = observer

    def apply_settings_check_marks(self):
        self.compact_menu_item.state = 1 if self.app._compact_menu else 0
        for seconds, item in self.skip_items.items():
            item.state = 1 if seconds == self.app._skip_interval else 0
        for value, item in self.recent_size_items.items():
            item.state = 1 if value == self.app._recent_limit else 0

    @staticmethod
    def mark_option_alternate(menu_item):
        menu_item._menuitem.setAlternate_(True)
        menu_item._menuitem.setKeyEquivalentModifierMask_(
            getattr(AppKit, "NSEventModifierFlagOption", AppKit.NSAlternateKeyMask)
        )

    def rebuild_recent_menu(self):
        entries = self.app._recent_entries_for_menu()
        if self.recent_menu._menu is not None:
            self.recent_menu.clear()

        if not entries:
            placeholder = rumps.MenuItem("No recent items")
            placeholder.set_callback(None)
            self.recent_menu["recent_empty"] = placeholder
            self.install_recent_menu_delegate()
            return

        for index, entry in enumerate(entries):
            title = truncate_title(entry.title)
            play_item = rumps.MenuItem(
                title,
                callback=lambda _, key=entry.cache_key: self.app._play_recent_entry(key),
            )
            remove_item = rumps.MenuItem(
                f"✕ {title}",
                callback=lambda _, key=entry.cache_key: self.app._remove_recent_entry(key),
            )
            self.mark_option_alternate(remove_item)
            self.recent_menu[f"recent_play_{index}"] = play_item
            self.recent_menu[f"recent_remove_{index}"] = remove_item

        self.install_recent_menu_delegate()

    def update_seek_markers(self, elapsed=0, duration=0):
        current_segment = None
        if duration > 0:
            frac = max(0, min(elapsed, duration)) / duration
            current_segment = min(9, int(frac * len(self.seek_items)))
        for index, item in enumerate(self.seek_items):
            item.title = self.seek_label(index, current_segment)

    def set_progress_display(self, elapsed=None, duration=None):
        if elapsed is None:
            if not self.app.engine.is_active:
                _set_header_title(self.progress, "")
                self.update_seek_markers()
                return
            elapsed = self.app.engine.elapsed
        if duration is None:
            duration = self.app.engine.duration
        _set_header_title(
            self.progress,
            progress_bar(elapsed, duration, width=PROGRESS_BAR_WIDTH),
        )
        self.update_seek_markers(elapsed, duration)

    def set_now_playing(self, title, playback_mode):
        badge = "◌" if playback_mode == "stream" else "●"
        _set_header_title(self.now_playing, title, trailing=badge)

    def set_not_playing(self):
        _set_header_title(self.now_playing, "Not Playing")
