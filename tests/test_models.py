from yt_bar.models import PlaybackSession, PlayRequest, UICommand, UICommandKind


def test_ui_command_factories_create_typed_commands():
    assert UICommand.play().kind is UICommandKind.PLAY
    assert UICommand.stopped().kind is UICommandKind.STOPPED
    assert UICommand.pause().kind is UICommandKind.PAUSE
    assert UICommand.toggle().kind is UICommandKind.TOGGLE

    seek = UICommand.seek_delta("30")

    assert seek.kind is UICommandKind.SEEK_DELTA
    assert seek.delta_seconds == 30.0


def test_playback_session_groups_mutable_runtime_state():
    first = PlaybackSession(id=1, request=PlayRequest(url="a", duration=1))
    second = PlaybackSession(id=2, request=PlayRequest(url="b", duration=2))

    first.decoder.queue.put("chunk")
    first.schedule.buffers[7] = object()
    first.route.rebuild_pending = True
    first.seek_trace.id = 3

    assert second.decoder.queue.empty()
    assert second.schedule.buffers == {}
    assert not second.route.rebuild_pending
    assert second.seek_trace.id == 0
