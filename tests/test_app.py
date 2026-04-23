from types import SimpleNamespace

import yt_bar.app as app_module
from yt_bar.models import ResolvedItem, TrackInfo, UICommand


class FakePasteboard:
    def __init__(self, value):
        self.value = value
        self.requested_types = []

    def stringForType_(self, pasteboard_type):
        self.requested_types.append(pasteboard_type)
        return self.value


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


def make_app_stub():
    app = app_module.YTBar.__new__(app_module.YTBar)
    app.engine = SimpleNamespace(is_active=False)
    app.playback = SimpleNamespace(playback_mode_for_item=lambda item: "stream")
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
