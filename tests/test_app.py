from types import SimpleNamespace

import yt_bar.app as app_module
from yt_bar.models import MenuAction, MenuPlaylistTrackEntry, ResolvedItem, TrackInfo, UICommand
from yt_bar.playback import LOCAL_PLAYBACK_MODE


class FakePasteboard:
    def __init__(self, value):
        self.value = value
        self.requested_types = []

    def stringForType_(self, pasteboard_type):
        self.requested_types.append(pasteboard_type)
        return self.value


class FakeURL:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class FakeOpenPanel:
    def __init__(self, response, path):
        self.response = response
        self.path = path
        self.can_choose_files = None
        self.can_choose_directories = None
        self.allows_multiple_selection = None
        self.resolves_aliases = None
        self.begin_handler = None
        self.make_key_calls = 0
        self.order_front_regardless_calls = 0

    def setCanChooseFiles_(self, value):
        self.can_choose_files = value

    def setCanChooseDirectories_(self, value):
        self.can_choose_directories = value

    def setAllowsMultipleSelection_(self, value):
        self.allows_multiple_selection = value

    def setResolvesAliases_(self, value):
        self.resolves_aliases = value

    def beginWithCompletionHandler_(self, handler):
        self.begin_handler = handler
        handler(self.response)

    def makeKeyAndOrderFront_(self, _sender):
        self.make_key_calls += 1

    def orderFrontRegardless(self):
        self.order_front_regardless_calls += 1

    def URL(self):
        if self.path is None:
            return None
        return FakeURL(self.path)


class FakeAlert:
    def __init__(self, response, edited_value):
        self.response = response
        self.edited_value = edited_value
        self.message_text = None
        self.informative_text = None
        self.buttons = []
        self.accessory_view = None

    def init(self):
        return self

    def setMessageText_(self, value):
        self.message_text = value

    def setInformativeText_(self, value):
        self.informative_text = value

    def addButtonWithTitle_(self, value):
        self.buttons.append(value)

    def setAccessoryView_(self, value):
        self.accessory_view = value

    def runModal(self):
        if self.accessory_view is not None and self.edited_value is not None:
            self.accessory_view.setStringValue_(self.edited_value)
        return self.response


class FakeTextField:
    def __init__(self):
        self.frame = None
        self.value = ""

    def initWithFrame_(self, frame):
        self.frame = frame
        return self

    def setStringValue_(self, value):
        self.value = value

    def stringValue(self):
        return self.value


class FakeApplication:
    def __init__(self):
        self.activations = []
        self.policy = 1
        self.policy_changes = []

    def activateIgnoringOtherApps_(self, value):
        self.activations.append(bool(value))

    def activationPolicy(self):
        return self.policy

    def setActivationPolicy_(self, value):
        self.policy = int(value)
        self.policy_changes.append(int(value))


class FakeStatusItem:
    def __init__(self, button):
        self._button = button

    def button(self):
        return self._button


class FakeThread:
    instances = []

    def __init__(self, *args, target=None, daemon=None, **kwargs):
        self.target = target
        self.daemon = daemon
        self.started = False
        type(self).instances.append(self)

    def start(self):
        self.started = True
        self.target()


def make_resolved_item():
    return ResolvedItem(
        kind="video",
        id="video-1",
        title="Resolved Track",
        source_url="https://example.test/watch?v=1",
        tracks=[
            TrackInfo(
                id="track-1",
                title="Resolved Track",
                duration=123.0,
                source_url="https://example.test/audio",
                local_path="songs/track-1.opus",
            )
        ],
    )


def install_clipboard(monkeypatch, value):
    pasteboard = FakePasteboard(value)

    class FakeNSPasteboard:
        @staticmethod
        def generalPasteboard():
            return pasteboard

    monkeypatch.setattr(
        app_module,
        "AppKit",
        SimpleNamespace(NSPasteboard=FakeNSPasteboard, NSStringPboardType="public.utf8"),
    )
    return pasteboard


def install_open_panel(monkeypatch, *, response=1, path=None):
    panel = FakeOpenPanel(response, path)
    application = FakeApplication()

    class FakeNSOpenPanel:
        @staticmethod
        def openPanel():
            return panel

    class FakeNSApplication:
        @staticmethod
        def sharedApplication():
            return application

    monkeypatch.setattr(
        app_module,
        "AppKit",
        SimpleNamespace(
            NSOpenPanel=FakeNSOpenPanel,
            NSApplication=FakeNSApplication,
            NSApplicationActivationPolicyRegular=0,
            NSModalResponseOK=1,
        ),
    )
    return panel, application


def install_rename_prompt(monkeypatch, *, response=1000, edited_value=None):
    application = FakeApplication()
    alert = FakeAlert(response, edited_value)
    text_field = FakeTextField()

    class FakeNSAlert:
        @staticmethod
        def alloc():
            return alert

    class FakeNSTextField:
        @staticmethod
        def alloc():
            return text_field

    class FakeNSApplication:
        @staticmethod
        def sharedApplication():
            return application

    monkeypatch.setattr(
        app_module,
        "AppKit",
        SimpleNamespace(
            NSAlert=FakeNSAlert,
            NSTextField=FakeNSTextField,
            NSApplication=FakeNSApplication,
            NSApplicationActivationPolicyRegular=0,
            NSAlertFirstButtonReturn=1000,
            NSMakeRect=lambda x, y, w, h: (x, y, w, h),
        ),
    )
    return alert, text_field, application


def make_app_stub():
    app = app_module.YTBar.__new__(app_module.YTBar)
    app.engine = SimpleNamespace(is_active=False, is_paused=False)
    app.playback = SimpleNamespace(
        playback_mode_for_item=lambda item: "stream",
        has_current_track=lambda: False,
        current_playlist_tracks=lambda: (),
    )
    app._local_file_panel = None
    app._local_file_picker_timer = None
    app._local_file_picker_timer_target = None
    app._local_file_panel_activation_policy = None
    app._recent_rename_timer = None
    app._recent_rename_timer_target = None
    app._pending_recent_rename_key = None
    app._now_playing_title = "Not Playing"
    app._now_playing_playback_mode = None
    app._progress_elapsed = None
    app._progress_duration = None
    app._show_play_pause = True
    app._show_seek = True
    app._show_songs = True
    app._skip_interval = 30.0
    app._recent_limit = 10
    app._render_menu = lambda: None
    app.recent = SimpleNamespace(
        display_title_for_recent=lambda _key: None,
        rename=lambda _key, _title: False,
        menu_entries=lambda _limit: (),
    )
    queued = []
    started = []

    def start_item_playback(item, *, playback_mode):
        started.append((item, playback_mode))

    app._enqueue_ui_action = queued.append
    app._start_item_playback = start_item_playback
    return app, queued, started


def test_on_paste_url_resolves_clipboard_url_and_starts_playback(monkeypatch):
    app, queued, started = make_app_stub()
    pasteboard = install_clipboard(monkeypatch, r"https:\/\/example.test/watch?v=123")
    item = make_resolved_item()
    resolved_urls = []
    FakeThread.instances = []

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        app_module,
        "resolve_url_result",
        lambda url: resolved_urls.append(url) or SimpleNamespace(item=item, error_text=None),
    )

    app.on_paste_url(None)

    assert pasteboard.requested_types == ["public.utf8"]
    assert resolved_urls == ["https://example.test/watch?v=123"]
    assert started == [(item, "stream")]
    assert queued == []
    assert len(FakeThread.instances) == 1
    assert FakeThread.instances[0].daemon is True
    assert FakeThread.instances[0].started is True


def test_on_paste_url_ignores_non_http_clipboard_content(monkeypatch):
    app, queued, started = make_app_stub()
    install_clipboard(monkeypatch, "not-a-url")
    FakeThread.instances = []
    resolved_urls = []

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        app_module,
        "resolve_url_result",
        lambda url: resolved_urls.append(url),
    )

    app.on_paste_url(None)

    assert FakeThread.instances == []
    assert resolved_urls == []
    assert started == []
    assert queued == []


def test_on_paste_url_enqueues_stopped_when_resolution_fails_and_engine_is_idle(monkeypatch):
    app, queued, started = make_app_stub()
    install_clipboard(monkeypatch, "https://example.test/missing")
    FakeThread.instances = []
    error_text = "ERROR: [youtube] Sign in to confirm you're not a bot"

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        app_module,
        "resolve_url_result",
        lambda url: SimpleNamespace(item=None, error_text=error_text),
    )

    app.on_paste_url(None)

    assert started == []
    assert queued == [
        UICommand.notify(
            "Clipboard Play Failed",
            "YouTube blocked resolution",
            error_text,
        ),
        UICommand.stopped(),
    ]
    assert len(FakeThread.instances) == 1


def test_on_paste_url_notifies_without_stopping_when_resolution_fails_during_playback(monkeypatch):
    app, queued, started = make_app_stub()
    app.engine = SimpleNamespace(is_active=True, is_paused=False)
    install_clipboard(monkeypatch, "https://example.test/missing")
    FakeThread.instances = []
    error_text = "temporary resolver failure"

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        app_module,
        "resolve_url_result",
        lambda url: SimpleNamespace(item=None, error_text=error_text),
    )

    app.on_paste_url(None)

    assert started == []
    assert queued == [
        UICommand.notify(
            "Clipboard Play Failed",
            "Couldn't resolve URL",
            error_text,
        )
    ]
    assert len(FakeThread.instances) == 1


def test_perform_ui_action_sends_notification(monkeypatch):
    app, _, _ = make_app_stub()
    notifications = []

    monkeypatch.setattr(
        app_module,
        "rumps",
        SimpleNamespace(
            notification=lambda title, subtitle, message: notifications.append(
                (title, subtitle, message)
            )
        ),
    )

    app._perform_ui_action(UICommand.notify("Clipboard Play Failed", "Subtitle", "Message"))

    assert notifications == [("Clipboard Play Failed", "Subtitle", "Message")]


def test_resume_pause_and_toggle_paths_sync_remote_state():
    app, _, _ = make_app_stub()
    toggle_calls = []
    sync_calls = []
    app.engine = SimpleNamespace(
        is_active=True, is_paused=True, toggle_pause=lambda: toggle_calls.append("toggle")
    )
    app._sync_now_playing_info = lambda: sync_calls.append("sync")

    assert app._play_or_resume_current_track() is True

    app.engine.is_paused = False
    assert app._pause_current_track() is True
    assert app._toggle_play_pause() is True

    assert toggle_calls == ["toggle", "toggle", "toggle"]
    assert sync_calls == ["sync", "sync", "sync"]


def test_handle_stopped_ui_clears_remote_state():
    app, _, _ = make_app_stub()
    progress_calls = []
    clear_calls = []
    app.engine = SimpleNamespace(is_active=False, is_paused=False)
    app._now_playing_title = "Current track"
    app._now_playing_playback_mode = "stream"
    app._set_progress_display = lambda elapsed=None, duration=None: progress_calls.append(
        (elapsed, duration)
    )
    app._clear_now_playing_info = lambda: clear_calls.append("clear")

    app._handle_stopped_ui()

    assert app._now_playing_title == "Not Playing"
    assert app._now_playing_playback_mode is None
    assert progress_calls == [(None, None)]
    assert clear_calls == ["clear"]


def test_on_play_local_file_imports_selection_and_starts_playback(monkeypatch):
    app, queued, started = make_app_stub()
    panel, application = install_open_panel(monkeypatch, path="/tmp/song.mp3")
    item = make_resolved_item()
    imported_paths = []
    FakeThread.instances = []
    timer_calls = []

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        app_module,
        "import_local_file",
        lambda path: imported_paths.append(path) or item,
    )
    monkeypatch.setattr(
        app_module,
        "schedule_default_mode_timer_once",
        lambda delay, callback: timer_calls.append(delay) or callback() or ("timer", "target"),
    )

    app.on_play_local_file(None)

    assert timer_calls == [0.0]
    assert panel.can_choose_files is True
    assert panel.can_choose_directories is False
    assert panel.allows_multiple_selection is False
    assert panel.resolves_aliases is True
    assert panel.begin_handler is not None
    assert panel.make_key_calls == 1
    assert panel.order_front_regardless_calls == 1
    assert application.policy_changes == [0, 1]
    assert application.activations == [True]
    assert imported_paths == ["/tmp/song.mp3"]
    assert started == [(item, LOCAL_PLAYBACK_MODE)]
    assert queued == []
    assert len(FakeThread.instances) == 1
    assert FakeThread.instances[0].daemon is True
    assert FakeThread.instances[0].started is True


def test_on_play_local_file_ignores_picker_cancel(monkeypatch):
    app, queued, started = make_app_stub()
    panel, application = install_open_panel(monkeypatch, response=0)
    FakeThread.instances = []
    imported_paths = []
    timer_calls = []

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(
        app_module,
        "import_local_file",
        lambda path: imported_paths.append(path),
    )
    monkeypatch.setattr(
        app_module,
        "schedule_default_mode_timer_once",
        lambda delay, callback: timer_calls.append(delay) or callback() or ("timer", "target"),
    )

    app.on_play_local_file(None)

    assert timer_calls == [0.0]
    assert panel.begin_handler is not None
    assert panel.make_key_calls == 1
    assert panel.order_front_regardless_calls == 1
    assert application.policy_changes == [0, 1]
    assert application.activations == [True]
    assert FakeThread.instances == []
    assert imported_paths == []
    assert started == []
    assert queued == []


def test_song_picker_entries_follow_current_playlist_tracks():
    app, _, _ = make_app_stub()
    tracks = [
        TrackInfo(
            id="track-1",
            title="Track One",
            duration=123.0,
            source_url="https://example.test/audio-1",
            local_path="songs/track-1.opus",
        ),
        TrackInfo(
            id="track-2",
            title="Track Two",
            duration=234.0,
            source_url="https://example.test/audio-2",
            local_path="songs/track-2.opus",
        ),
    ]
    app.playback = SimpleNamespace(
        has_current_track=lambda: True,
        current_playlist_tracks=lambda: tuple(tracks),
    )

    assert app._song_picker_entries() == (
        MenuPlaylistTrackEntry(index=0, title="Track One"),
        MenuPlaylistTrackEntry(index=1, title="Track Two"),
    )


def test_menu_snapshot_includes_song_picker_state():
    app, _, _ = make_app_stub()
    app._now_playing_title = "Current track"
    app._now_playing_playback_mode = "stream"
    app._progress_elapsed = 12.5
    app._progress_duration = 100.0
    app._show_play_pause = False
    app._show_seek = True
    app._show_songs = False
    app._skip_interval = 45.0
    app._recent_limit = 5
    app.playback = SimpleNamespace(
        has_current_track=lambda: True,
        current_playlist_tracks=lambda: (
            TrackInfo(
                id="track-1",
                title="Track One",
                duration=123.0,
                source_url="https://example.test/audio-1",
                local_path="songs/track-1.opus",
            ),
            TrackInfo(
                id="track-2",
                title="Track Two",
                duration=234.0,
                source_url="https://example.test/audio-2",
                local_path="songs/track-2.opus",
            ),
        ),
    )
    app.recent = SimpleNamespace(menu_entries=lambda _limit: ())

    snapshot = app._menu_snapshot()

    assert snapshot.show_play_pause is False
    assert snapshot.show_seek is True
    assert snapshot.show_songs is False
    assert snapshot.song_picker_enabled is True
    assert snapshot.song_picker_entries == (
        MenuPlaylistTrackEntry(index=0, title="Track One"),
        MenuPlaylistTrackEntry(index=1, title="Track Two"),
    )


def test_song_picker_action_starts_selected_playlist_track():
    app, _, _ = make_app_stub()
    play_calls = []
    render_calls = []
    app.playback = SimpleNamespace(current_playlist_tracks=lambda: ("first", "second"))
    app._play_track = lambda index, start_time=0, paused=False: play_calls.append(
        (index, start_time, paused)
    )
    app._render_menu = lambda: render_calls.append("render")

    app._handle_menu_action(MenuAction.play_current_playlist_track(1))

    assert play_calls == [(1, 0, False)]
    assert render_calls == ["render"]


def test_transport_visibility_actions_toggle_settings():
    app, _, _ = make_app_stub()
    save_calls = []
    render_calls = []
    app._save_settings = lambda: save_calls.append(
        (app._show_play_pause, app._show_seek, app._show_songs)
    )
    app._render_menu = lambda: render_calls.append(
        (app._show_play_pause, app._show_seek, app._show_songs)
    )

    app._handle_menu_action(MenuAction.toggle_show_play_pause())
    app._handle_menu_action(MenuAction.toggle_show_seek())
    app._handle_menu_action(MenuAction.toggle_show_songs())

    assert save_calls == [
        (False, True, True),
        (False, False, True),
        (False, False, False),
    ]
    assert render_calls == [
        (False, True, True),
        (False, True, True),
        (False, False, True),
        (False, False, True),
        (False, False, False),
        (False, False, False),
    ]


def test_compact_menu_action_toggles_all_transport_visibility_flags():
    app, _, _ = make_app_stub()
    save_calls = []
    render_calls = []
    app._show_play_pause = True
    app._show_seek = False
    app._show_songs = True
    app._save_settings = lambda: save_calls.append(
        (app._show_play_pause, app._show_seek, app._show_songs)
    )
    app._render_menu = lambda: render_calls.append(
        (app._show_play_pause, app._show_seek, app._show_songs)
    )

    app._handle_menu_action(MenuAction.toggle_compact_menu())
    app._handle_menu_action(MenuAction.toggle_compact_menu())

    assert save_calls == [
        (False, False, False),
        (True, True, True),
    ]
    assert render_calls == [
        (False, False, False),
        (False, False, False),
        (True, True, True),
        (True, True, True),
    ]


def test_rename_recent_action_prompts_and_saves_override(monkeypatch):
    app, _, _ = make_app_stub()
    alert, text_field, application = install_rename_prompt(
        monkeypatch,
        edited_value="Custom Label",
    )
    rename_calls = []
    render_calls = []
    timer_calls = []

    app.recent = SimpleNamespace(
        display_title_for_recent=lambda key: "Original Title" if key == "video:1" else None,
        rename=lambda key, title: rename_calls.append((key, title)) or True,
    )
    app._render_menu = lambda: render_calls.append("render")

    monkeypatch.setattr(
        app_module,
        "schedule_default_mode_timer_once",
        lambda delay, callback: timer_calls.append(delay) or callback() or ("timer", "target"),
    )

    app._handle_menu_action(MenuAction.rename_recent("video:1"))

    assert timer_calls == [0.0]
    assert alert.message_text == "Rename Recent"
    assert alert.buttons == ["Save", "Cancel"]
    assert text_field.frame == (0, 0, 320, 24)
    assert text_field.value == "Custom Label"
    assert rename_calls == [("video:1", "Custom Label")]
    assert application.policy_changes == [0, 1]
    assert application.activations == [True]
    assert render_calls == ["render", "render"]


def test_rename_recent_action_cancel_leaves_entry_unchanged(monkeypatch):
    app, _, _ = make_app_stub()
    _, text_field, application = install_rename_prompt(monkeypatch, response=0)
    rename_calls = []
    timer_calls = []
    render_calls = []

    app.recent = SimpleNamespace(
        display_title_for_recent=lambda key: "Original Title" if key == "video:1" else None,
        rename=lambda key, title: rename_calls.append((key, title)) or True,
    )
    app._render_menu = lambda: render_calls.append("render")

    monkeypatch.setattr(
        app_module,
        "schedule_default_mode_timer_once",
        lambda delay, callback: timer_calls.append(delay) or callback() or ("timer", "target"),
    )

    app._handle_menu_action(MenuAction.rename_recent("video:1"))

    assert timer_calls == [0.0]
    assert text_field.value == "Original Title"
    assert rename_calls == []
    assert application.policy_changes == [0, 1]
    assert application.activations == [True]
    assert render_calls == ["render"]


def test_install_status_item_file_drop_registers_button_callback(monkeypatch):
    app, _, _ = make_app_stub()
    button = object()
    dropped_paths = []
    install_calls = []
    app._nsapp = SimpleNamespace(nsstatusitem=FakeStatusItem(button))
    app._handle_dropped_local_file = dropped_paths.append

    monkeypatch.setattr(
        app_module,
        "install_status_item_file_drop",
        lambda target, callback: install_calls.append((target, callback)),
    )

    app._install_status_item_file_drop()

    assert len(install_calls) == 1
    assert install_calls[0][0] is button

    install_calls[0][1]("/tmp/song.mp3")
    assert dropped_paths == ["/tmp/song.mp3"]


def test_install_status_item_file_drop_ignores_missing_button(monkeypatch):
    app, _, _ = make_app_stub()
    install_calls = []
    app._nsapp = SimpleNamespace(nsstatusitem=FakeStatusItem(None))

    monkeypatch.setattr(
        app_module,
        "install_status_item_file_drop",
        lambda target, callback: install_calls.append((target, callback)),
    )

    app._install_status_item_file_drop()

    assert install_calls == []


def test_handle_dropped_local_file_uses_existing_async_import(monkeypatch):
    app, _, _ = make_app_stub()
    imported_paths = []
    app._import_local_file_async = imported_paths.append

    app._handle_dropped_local_file("/tmp/song.mp3")
    app._handle_dropped_local_file("")

    assert imported_paths == ["/tmp/song.mp3"]
