from types import SimpleNamespace

import yt_bar.remote_commands as remote_commands_module
from yt_bar.models import TrackInfo, UICommand


class FakeRemoteCommand:
    def __init__(self, name):
        self.name = name
        self.target = None
        self.action = None
        self.preferred_intervals = None
        self.enabled = None
        self.removed_targets = []

    def addTarget_action_(self, target, action):
        self.target = target
        self.action = action

    def removeTarget_(self, target):
        self.removed_targets.append(target)
        if self.target is target:
            self.target = None

    def setPreferredIntervals_(self, intervals):
        self.preferred_intervals = list(intervals)

    def setEnabled_(self, enabled):
        self.enabled = bool(enabled)

    def fire(self):
        assert self.target is not None
        assert self.action is not None
        method_name = self.action.replace(":", "_")
        return getattr(self.target, method_name)(None)


class FakeCommandCenter:
    def __init__(self):
        self.play = FakeRemoteCommand("play")
        self.pause = FakeRemoteCommand("pause")
        self.toggle = FakeRemoteCommand("toggle")
        self.skip_forward = FakeRemoteCommand("skip_forward")
        self.skip_backward = FakeRemoteCommand("skip_backward")
        self.next_track = FakeRemoteCommand("next_track")
        self.previous_track = FakeRemoteCommand("previous_track")

    def playCommand(self):
        return self.play

    def pauseCommand(self):
        return self.pause

    def togglePlayPauseCommand(self):
        return self.toggle

    def skipForwardCommand(self):
        return self.skip_forward

    def skipBackwardCommand(self):
        return self.skip_backward

    def nextTrackCommand(self):
        return self.next_track

    def previousTrackCommand(self):
        return self.previous_track


class FakeNowPlayingInfoCenter:
    def __init__(self):
        self.set_calls = []
        self.playback_state_calls = []

    def setNowPlayingInfo_(self, info):
        self.set_calls.append(info)

    def setPlaybackState_(self, playback_state):
        self.playback_state_calls.append(playback_state)


class FakeRemoteCommandBridge:
    @classmethod
    def alloc(cls):
        return cls()

    def initWithOwner_(self, owner):
        self.owner = owner
        return self

    def handlePlayCommand_(self, event):
        return self.owner._handle_remote_play_command()

    def handlePauseCommand_(self, event):
        return self.owner._handle_remote_pause_command()

    def handleTogglePlayPauseCommand_(self, event):
        return self.owner._handle_remote_toggle_command()

    def handleSkipForwardCommand_(self, event):
        return self.owner._handle_remote_skip_forward_command()

    def handleSkipBackwardCommand_(self, event):
        return self.owner._handle_remote_skip_backward_command()

    def handleNextTrackCommand_(self, event):
        return self.owner._handle_remote_skip_forward_command()

    def handlePreviousTrackCommand_(self, event):
        return self.owner._handle_remote_skip_backward_command()


def make_controller(
    monkeypatch,
    *,
    engine=None,
    current_track=None,
    current_track_snapshot=None,
    skip_interval=lambda: 30.0,
):
    command_center = FakeCommandCenter()
    info_center = FakeNowPlayingInfoCenter()

    class FakeCommandCenterClass:
        @classmethod
        def sharedCommandCenter(cls):
            return command_center

    class FakeNowPlayingInfoCenterClass:
        @classmethod
        def defaultCenter(cls):
            return info_center

    support = SimpleNamespace(
        command_center_class=FakeCommandCenterClass,
        now_playing_info_center_class=FakeNowPlayingInfoCenterClass,
        command_status_success=0,
        command_status_command_failed=200,
        command_status_no_such_content=100,
        playback_state_playing=1,
        playback_state_stopped=3,
        property_elapsed_playback_time="elapsed",
        property_playback_rate="rate",
        property_title="title",
        property_playback_duration="duration",
    )

    monkeypatch.setattr(remote_commands_module, "load_media_player_support", lambda: support)
    monkeypatch.setattr(remote_commands_module, "RemoteCommandBridge", FakeRemoteCommandBridge)

    queued = []
    controller = remote_commands_module.RemoteCommandController(
        engine=engine
        or SimpleNamespace(is_active=False, is_paused=False, duration=0.0, elapsed=0.0),
        enqueue_ui_action=queued.append,
        current_track=current_track or (lambda: None),
        current_track_snapshot=current_track_snapshot or (lambda: (-1, None)),
        skip_interval=skip_interval,
    )
    return controller, command_center, info_center, queued


def make_track():
    return TrackInfo(
        id="track-1",
        title="Remote Track",
        duration=123.0,
        source_url="https://example.test/audio",
        local_path="songs/track-1.opus",
    )


def test_remote_command_handlers_register_and_dispatch_expected_ui_commands(monkeypatch):
    track = make_track()
    engine = SimpleNamespace(is_active=True, is_paused=True, duration=120.0, elapsed=15.0)
    _, center, _, queued = make_controller(
        monkeypatch,
        engine=engine,
        current_track=lambda: track,
        current_track_snapshot=lambda: (0, track),
        skip_interval=lambda: 45.0,
    )

    assert center.skip_forward.preferred_intervals == [45.0]
    assert center.skip_backward.preferred_intervals == [45.0]
    assert center.next_track.enabled is True
    assert center.previous_track.enabled is True

    assert center.play.fire() == 0
    engine.is_paused = False
    assert center.pause.fire() == 0
    assert center.toggle.fire() == 0
    assert center.skip_forward.fire() == 0
    assert center.skip_backward.fire() == 0
    assert center.next_track.fire() == 0
    assert center.previous_track.fire() == 0

    assert queued == [
        UICommand.play(),
        UICommand.pause(),
        UICommand.toggle(),
        UICommand.seek_delta(45.0),
        UICommand.seek_delta(-45.0),
        UICommand.seek_delta(45.0),
        UICommand.seek_delta(-45.0),
    ]


def test_remote_command_handlers_return_no_such_content_without_active_track(monkeypatch):
    _, center, _, queued = make_controller(monkeypatch)

    assert center.play.fire() == 100
    assert center.pause.fire() == 100
    assert center.toggle.fire() == 100
    assert center.skip_forward.fire() == 100
    assert center.skip_backward.fire() == 100
    assert queued == []


def test_sync_now_playing_info_only_publishes_while_playing(monkeypatch):
    track = make_track()
    engine = SimpleNamespace(is_active=True, is_paused=True, duration=0.0, elapsed=37.5)
    controller, _, info_center, _ = make_controller(
        monkeypatch,
        engine=engine,
        current_track=lambda: track,
        current_track_snapshot=lambda: (0, track),
    )

    controller.sync_now_playing_info()

    assert info_center.set_calls[-1] is None
    assert info_center.playback_state_calls[-1] == 3

    engine.is_paused = False
    controller.sync_now_playing_info()

    assert info_center.set_calls[-1] == {
        "title": "Remote Track",
        "elapsed": 37.5,
        "rate": 1.0,
        "duration": 123.0,
    }
    assert info_center.playback_state_calls[-1] == 1

    engine.is_active = False
    controller.sync_now_playing_info()

    assert info_center.set_calls[-1] is None
    assert info_center.playback_state_calls[-1] == 3


def test_close_stops_and_clears_now_playing_state(monkeypatch):
    track = make_track()
    engine = SimpleNamespace(is_active=True, is_paused=False, duration=123.0, elapsed=5.0)
    controller, _, info_center, _ = make_controller(
        monkeypatch,
        engine=engine,
        current_track=lambda: track,
        current_track_snapshot=lambda: (0, track),
    )

    controller.sync_now_playing_info()
    controller.close()

    assert info_center.set_calls[-1] is None
    assert info_center.playback_state_calls[-1] == 3
