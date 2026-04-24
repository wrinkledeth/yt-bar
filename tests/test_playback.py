from yt_bar.models import ResolvedItem, TrackInfo
from yt_bar.playback import (
    LOCAL_PLAYBACK_MODE,
    STREAM_PLAYBACK_MODE,
    PlaybackController,
)


def make_track(track_id, path, *, source_url=None, duration=10.0):
    return TrackInfo(
        id=track_id,
        title=track_id.title(),
        duration=duration,
        source_url=source_url if source_url is not None else f"https://example.test/{track_id}",
        local_path=str(path),
    )


def make_item(*tracks, kind="playlist", item_id="item"):
    return ResolvedItem(
        kind=kind,
        id=item_id,
        title=item_id.title(),
        source_url=f"https://example.test/{item_id}",
        tracks=list(tracks),
    )


def test_start_item_tracks_current_selection_generation_and_cache_intent(tmp_path):
    first = make_track("first", tmp_path / "first.opus")
    second = make_track("second", tmp_path / "second.opus")
    item = make_item(first, second)
    playback = PlaybackController()

    start = playback.start_item(item, playback_mode=STREAM_PLAYBACK_MODE)

    assert start.item is item
    assert start.generation == 1
    assert start.should_cache is True
    assert playback.current_track() is first
    assert playback.current_track_snapshot() == (0, first)
    assert playback.has_current_track() is True
    assert playback.is_current_stream_item(item, start.generation) is True

    local_start = playback.start_item(item, playback_mode=LOCAL_PLAYBACK_MODE)

    assert local_start.generation == 2
    assert local_start.should_cache is False
    assert playback.is_current_stream_item(item, local_start.generation) is False
    assert playback.is_current_stream_item(item, start.generation) is False


def test_playback_mode_for_item_uses_local_only_when_fully_cached(tmp_path):
    cached_path = tmp_path / "cached.opus"
    cached_path.write_bytes(b"cached")
    cached = make_track("cached", cached_path)
    missing = make_track("missing", tmp_path / "missing.opus")

    assert (
        PlaybackController.playback_mode_for_item(make_item(cached, kind="video"))
        == LOCAL_PLAYBACK_MODE
    )
    assert (
        PlaybackController.playback_mode_for_item(make_item(cached, missing))
        == STREAM_PLAYBACK_MODE
    )


def test_current_playlist_tracks_only_exposes_multi_track_playlists(tmp_path):
    first = make_track("first", tmp_path / "first.opus")
    second = make_track("second", tmp_path / "second.opus")
    playback = PlaybackController()

    playback.start_item(make_item(first, second), playback_mode=STREAM_PLAYBACK_MODE)
    assert playback.current_playlist_tracks() == (first, second)

    playback.start_item(make_item(first, kind="playlist"), playback_mode=STREAM_PLAYBACK_MODE)
    assert playback.current_playlist_tracks() == ()

    playback.start_item(
        make_item(first, second, kind="video"),
        playback_mode=STREAM_PLAYBACK_MODE,
    )
    assert playback.current_playlist_tracks() == ()


def test_select_track_returns_stream_or_local_playback_source(tmp_path):
    local_path = tmp_path / "track.opus"
    track = make_track("track", local_path)
    item = make_item(track)
    playback = PlaybackController()

    playback.start_item(item, playback_mode=STREAM_PLAYBACK_MODE)
    streamed = playback.select_track(0)

    assert streamed is not None
    assert streamed.track is track
    assert streamed.playback_mode == STREAM_PLAYBACK_MODE
    assert streamed.source == track.source_url
    assert streamed.is_local is False

    playback.start_item(item, playback_mode=LOCAL_PLAYBACK_MODE)
    local = playback.select_track(0)

    assert local is not None
    assert local.playback_mode == LOCAL_PLAYBACK_MODE
    assert local.source == str(local_path)
    assert local.is_local is True


def test_track_advancement_moves_through_playlist_and_stops_at_end(tmp_path):
    first = make_track("first", tmp_path / "first.opus")
    second = make_track("second", tmp_path / "second.opus")
    playback = PlaybackController()
    playback.start_item(make_item(first, second), playback_mode=STREAM_PLAYBACK_MODE)

    advance = playback.advance_after_track_finished()

    assert advance.has_next_track is True
    assert advance.next_index == 1
    assert playback.current_track_snapshot() == (1, second)

    final_advance = playback.advance_after_track_finished()

    assert final_advance.has_next_track is False
    assert final_advance.next_index is None
    assert playback.current_track_snapshot() == (1, second)


def test_invalid_track_selection_leaves_current_track_unchanged(tmp_path):
    track = make_track("track", tmp_path / "track.opus")
    playback = PlaybackController()
    playback.start_item(make_item(track), playback_mode=STREAM_PLAYBACK_MODE)

    assert playback.select_track(-1) is None
    assert playback.select_track(1) is None
    assert playback.current_track_snapshot() == (0, track)
