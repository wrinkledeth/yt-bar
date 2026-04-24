from types import SimpleNamespace

import yt_bar.app as app_module
from yt_bar.models import ResolvedItem, TrackInfo, UICommand
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


def make_app_stub():
    app = app_module.YTBar.__new__(app_module.YTBar)
    app.engine = SimpleNamespace(is_active=False)
    app.playback = SimpleNamespace(playback_mode_for_item=lambda item: "stream")
    app._local_file_panel = None
    app._local_file_picker_timer = None
    app._local_file_picker_timer_target = None
    app._local_file_panel_activation_policy = None
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
        "resolve_url",
        lambda url: resolved_urls.append(url) or item,
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
        "resolve_url",
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

    monkeypatch.setattr(app_module.threading, "Thread", FakeThread)
    monkeypatch.setattr(app_module, "resolve_url", lambda url: None)

    app.on_paste_url(None)

    assert started == []
    assert queued == [UICommand.stopped()]
    assert len(FakeThread.instances) == 1


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
