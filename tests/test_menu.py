from types import SimpleNamespace

import yt_bar.menu as menu_module
from yt_bar.models import MenuAction, MenuPlaylistTrackEntry, MenuRecentEntry, MenuSnapshot


class FakeNativeMenuItem:
    def __init__(self):
        self.enabled = True
        self.alternate = False
        self.modifier_mask = None
        self.attributed_title = None

    def setEnabled_(self, enabled):
        self.enabled = bool(enabled)

    def isEnabled(self):
        return self.enabled

    def setAlternate_(self, alternate):
        self.alternate = bool(alternate)

    def setKeyEquivalentModifierMask_(self, mask):
        self.modifier_mask = mask

    def setAttributedTitle_(self, value):
        self.attributed_title = value


class FakeMenu:
    def __init__(self):
        self.delegate = None

    def setDelegate_(self, delegate):
        self.delegate = delegate


class FakeMenuItem:
    def __init__(self, title, callback=None):
        self.title = title
        self.callback = callback
        self.state = 0
        self.children = {}
        self._menuitem = FakeNativeMenuItem()
        self._menu = FakeMenu()

    def set_callback(self, callback):
        self.callback = callback

    def clear(self):
        self.children.clear()

    def __setitem__(self, key, value):
        self.children[key] = value


class FakeRecentMenuObserver:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithCallback_(self, callback):
        self.callback = callback
        return self


def install_menu_fakes(monkeypatch):
    monkeypatch.setattr(menu_module, "rumps", SimpleNamespace(MenuItem=FakeMenuItem))
    monkeypatch.setattr(
        menu_module,
        "AppKit",
        SimpleNamespace(
            NSEventModifierFlagOption=524288,
            NSAlternateKeyMask=524288,
            NSEventModifierFlagShift=131072,
            NSShiftKeyMask=131072,
        ),
    )
    monkeypatch.setattr(menu_module, "RecentMenuObserver", FakeRecentMenuObserver)

    def fake_set_header_title(menu_item, primary, trailing=None):
        menu_item._menuitem.setAttributedTitle_({"primary": primary, "trailing": trailing})

    monkeypatch.setattr(menu_module, "_set_header_title", fake_set_header_title)


def make_controller(monkeypatch):
    install_menu_fakes(monkeypatch)
    actions = []
    layouts = []
    controller = menu_module.MenuController(
        dispatch_action=actions.append,
        apply_layout=layouts.append,
    )
    return controller, actions, layouts


def test_render_builds_full_menu_and_dispatches_callbacks(monkeypatch):
    controller, actions, layouts = make_controller(monkeypatch)
    snapshot = MenuSnapshot(
        now_playing_title="Boundary Song",
        playback_mode="stream",
        progress_elapsed=30,
        progress_duration=100,
        active=True,
        paused=False,
        has_current_track=True,
        show_play_pause=True,
        show_seek=True,
        show_songs=True,
        skip_interval=60.0,
        recent_limit=10,
        song_picker_enabled=True,
        song_picker_entries=(
            MenuPlaylistTrackEntry(index=0, title="Track One"),
            MenuPlaylistTrackEntry(index=1, title="Track Two"),
        ),
        recent_entries=(MenuRecentEntry(cache_key="video:1", title="First Recent"),),
    )

    controller.render(snapshot)

    assert layouts[-1] == [
        controller.now_playing,
        controller.progress,
        None,
        controller.paste_item,
        controller.local_file_item,
        controller.recent_menu,
        None,
        controller.playpause_item,
        controller.seek_menu,
        controller.song_picker_menu,
        None,
        controller.settings_menu,
    ]
    assert controller.now_playing._menuitem.attributed_title == {
        "primary": "Boundary Song",
        "trailing": "◌",
    }
    assert controller.progress._menuitem.attributed_title["primary"].endswith("0:30 / 1:40")
    assert controller.playpause_item.title == "Pause"
    assert controller.playpause_item.callback is not None
    assert controller.playpause_item._menuitem.enabled is True
    assert controller.seek_menu._menuitem.enabled is True
    assert controller.song_picker_menu._menuitem.enabled is True
    assert controller.song_picker_menu.children["song_picker_1"].title == "Track Two"
    assert controller.seek_items[3].title == "  ● 30%"
    assert controller.show_play_pause_item.state == 1
    assert controller.show_seek_item.state == 1
    assert controller.show_songs_item.state == 1
    assert controller.skip_items[60.0].state == 1
    assert controller.recent_size_items[10].state == 1
    assert controller.recent_menu._menu.delegate is controller._recent_menu_observer
    assert controller.recent_menu.children["recent_rename_0"].title == "Rename…"
    assert controller.recent_menu.children["recent_rename_0"]._menuitem.alternate is True
    assert controller.recent_menu.children["recent_rename_0"]._menuitem.modifier_mask == 655360
    assert controller.recent_menu.children["recent_remove_0"]._menuitem.alternate is True
    assert controller.recent_menu.children["recent_remove_0"]._menuitem.modifier_mask == 524288

    controller.paste_item.callback(None)
    controller.local_file_item.callback(None)
    controller.playpause_item.callback(None)
    controller.seek_items[3].callback(None)
    controller.song_picker_menu.children["song_picker_1"].callback(None)
    controller.recent_menu.children["recent_play_0"].callback(None)
    controller.recent_menu.children["recent_rename_0"].callback(None)
    controller.recent_menu.children["recent_remove_0"].callback(None)
    controller.show_play_pause_item.callback(None)
    controller.show_seek_item.callback(None)
    controller.show_songs_item.callback(None)
    controller.skip_items[60.0].callback(None)
    controller.recent_size_items[10].callback(None)
    controller.recent_menu._menu.delegate.callback()

    assert actions == [
        MenuAction.play_from_clipboard(),
        MenuAction.play_local_file(),
        MenuAction.play_pause(),
        MenuAction.seek_percent(30),
        MenuAction.play_current_playlist_track(1),
        MenuAction.play_recent("video:1"),
        MenuAction.rename_recent("video:1"),
        MenuAction.remove_recent("video:1"),
        MenuAction.toggle_show_play_pause(),
        MenuAction.toggle_show_seek(),
        MenuAction.toggle_show_songs(),
        MenuAction.set_skip_interval(60.0),
        MenuAction.set_recent_limit(10),
        MenuAction.recent_menu_will_open(),
    ]


def test_render_hides_transport_items_and_shows_empty_recent_placeholder(monkeypatch):
    controller, _, layouts = make_controller(monkeypatch)
    snapshot = MenuSnapshot(
        now_playing_title="Not Playing",
        playback_mode=None,
        active=False,
        paused=False,
        has_current_track=False,
        show_play_pause=False,
        show_seek=False,
        show_songs=False,
        skip_interval=30.0,
        recent_limit=20,
        recent_entries=(),
    )

    controller.render(snapshot)

    assert layouts[-1] == [
        controller.now_playing,
        controller.progress,
        None,
        controller.paste_item,
        controller.local_file_item,
        controller.recent_menu,
        None,
        controller.settings_menu,
    ]
    assert controller.now_playing._menuitem.attributed_title == {
        "primary": "Not Playing",
        "trailing": None,
    }
    assert controller.progress._menuitem.attributed_title == {"primary": "", "trailing": None}
    assert controller.playpause_item.title == "Play"
    assert controller.playpause_item.callback is None
    assert controller.playpause_item._menuitem.enabled is False
    assert controller.seek_menu._menuitem.enabled is False
    assert controller.show_play_pause_item.state == 0
    assert controller.show_seek_item.state == 0
    assert controller.show_songs_item.state == 0
    assert controller.skip_items[30.0].state == 1
    assert controller.recent_size_items[20].state == 1
    assert controller.recent_menu.children["recent_empty"].title == "No recent items"
    assert controller.recent_menu.children["recent_empty"].callback is None


def test_render_disables_song_picker_without_multi_track_playlist(monkeypatch):
    controller, _, _ = make_controller(monkeypatch)
    snapshot = MenuSnapshot(
        now_playing_title="Current track",
        playback_mode="local",
        active=False,
        paused=False,
        has_current_track=True,
        show_play_pause=True,
        show_seek=True,
        show_songs=True,
        skip_interval=30.0,
        recent_limit=10,
        song_picker_enabled=False,
        song_picker_entries=(),
        recent_entries=(),
    )

    controller.render(snapshot)

    assert controller.song_picker_menu._menuitem.enabled is False
    assert controller.song_picker_menu.children["song_picker_empty"].title == "No songs available"
    assert controller.song_picker_menu.children["song_picker_empty"].callback is None
