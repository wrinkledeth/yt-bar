from yt_bar.models import (
    MenuAction,
    MenuActionKind,
    MenuRecentEntry,
    MenuSnapshot,
    PlaybackSession,
    PlayRequest,
    UICommand,
    UICommandKind,
)


def test_ui_command_factories_create_typed_commands():
    assert UICommand.play().kind is UICommandKind.PLAY
    assert UICommand.stopped().kind is UICommandKind.STOPPED
    assert UICommand.pause().kind is UICommandKind.PAUSE
    assert UICommand.toggle().kind is UICommandKind.TOGGLE

    seek = UICommand.seek_delta("30")

    assert seek.kind is UICommandKind.SEEK_DELTA
    assert seek.delta_seconds == 30.0


def test_menu_action_factories_create_typed_actions():
    assert MenuAction.play_from_clipboard().kind is MenuActionKind.PLAY_FROM_CLIPBOARD
    assert MenuAction.play_pause().kind is MenuActionKind.PLAY_PAUSE
    assert MenuAction.toggle_compact_menu().kind is MenuActionKind.TOGGLE_COMPACT_MENU
    assert MenuAction.recent_menu_will_open().kind is MenuActionKind.RECENT_MENU_WILL_OPEN

    seek = MenuAction.seek_percent("40")
    play_recent = MenuAction.play_recent("video:abc")
    remove_recent = MenuAction.remove_recent("playlist:def")
    skip = MenuAction.set_skip_interval("45")
    recent_limit = MenuAction.set_recent_limit("5")

    assert seek.kind is MenuActionKind.SEEK_PERCENT
    assert seek.percent == 40
    assert play_recent.kind is MenuActionKind.PLAY_RECENT
    assert play_recent.cache_key == "video:abc"
    assert remove_recent.kind is MenuActionKind.REMOVE_RECENT
    assert remove_recent.cache_key == "playlist:def"
    assert skip.kind is MenuActionKind.SET_SKIP_INTERVAL
    assert skip.seconds == 45.0
    assert recent_limit.kind is MenuActionKind.SET_RECENT_LIMIT
    assert recent_limit.recent_limit == 5


def test_menu_snapshot_carries_render_state_immutably():
    recent_entry = MenuRecentEntry(cache_key="video:1", title="A title")
    snapshot = MenuSnapshot(
        now_playing_title="Current track",
        playback_mode="stream",
        progress_elapsed=12.5,
        progress_duration=100.0,
        active=True,
        paused=False,
        has_current_track=True,
        compact_menu=True,
        skip_interval=45.0,
        recent_limit=5,
        recent_entries=(recent_entry,),
    )

    assert snapshot.now_playing_title == "Current track"
    assert snapshot.playback_mode == "stream"
    assert snapshot.recent_entries == (recent_entry,)


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
