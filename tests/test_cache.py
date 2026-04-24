from yt_bar.cache import CacheManager
from yt_bar.models import ResolvedItem, TrackInfo


def make_track(track_id, *, source_url=None, local_path=None):
    return TrackInfo(
        id=track_id,
        title=track_id.title(),
        duration=1.0,
        source_url=source_url if source_url is not None else f"https://example.test/{track_id}",
        local_path=local_path or f"songs/{track_id}.opus",
    )


def make_item(*tracks):
    return ResolvedItem(
        kind="video",
        id="item",
        title="Item",
        source_url="https://example.test/item",
        tracks=list(tracks),
    )


def test_enqueue_cache_jobs_refreshes_recent_and_skips_uncacheable_tracks(tmp_path):
    refresh_calls = []
    cached_path = tmp_path / "cached.opus"
    cached_path.write_bytes(b"cached")
    cached = make_track("cached", local_path=str(cached_path))
    no_source = make_track("no-source", source_url="")
    stream = make_track("stream", local_path=str(tmp_path / "stream.opus"))
    item = make_item(cached, no_source, stream)
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: True,
        refresh_recent_for_cache=refresh_calls.append,
        songs_dir=tmp_path,
        worker_count=0,
    )

    manager.enqueue_cache_jobs_for_item(item)

    assert refresh_calls == [item]
    assert manager._scheduled_track_ids == {"stream"}
    job = manager._jobs.get_nowait()
    assert job.item is item
    assert job.track is stream
    assert manager._jobs.empty()


def test_enqueue_cache_jobs_deduplicates_scheduled_tracks(tmp_path):
    refresh_calls = []
    track = make_track("same", local_path=str(tmp_path / "same.opus"))
    item = make_item(track)
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: True,
        refresh_recent_for_cache=refresh_calls.append,
        songs_dir=tmp_path,
        worker_count=0,
    )

    manager.enqueue_cache_jobs_for_item(item)
    manager.enqueue_cache_jobs_for_item(item)

    assert refresh_calls == [item, item]
    assert manager._jobs.qsize() == 1


def test_start_cache_if_still_current_honors_generation_check(tmp_path):
    refresh_calls = []
    item = make_item(make_track("track", local_path=str(tmp_path / "track.opus")))
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: False,
        refresh_recent_for_cache=refresh_calls.append,
        songs_dir=tmp_path,
        worker_count=0,
    )

    manager._start_cache_if_still_current(item, generation=3)

    assert refresh_calls == []
    assert manager._jobs.empty()


def test_download_track_cache_renames_partial_on_success(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "yt_bar.models.partial_cache_abspath_for_path",
        lambda local_path: str(tmp_path / "track.partial.opus"),
    )
    track = make_track("track", local_path=str(tmp_path / "track.opus"))
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: True,
        refresh_recent_for_cache=lambda item: None,
        songs_dir=tmp_path,
        worker_count=0,
    )

    def fake_run(args, capture_output, text):
        assert capture_output is True
        assert text is True
        assert args[-1] == track.source_url
        tmp_path.joinpath("track.partial.opus").write_bytes(b"audio")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr("yt_bar.cache.subprocess.run", fake_run)

    assert manager._download_track_cache(track) is True
    assert tmp_path.joinpath("track.opus").read_bytes() == b"audio"
    assert not tmp_path.joinpath("track.partial.opus").exists()


def test_download_track_cache_removes_partial_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "yt_bar.models.partial_cache_abspath_for_path",
        lambda local_path: str(tmp_path / "track.partial.opus"),
    )
    track = make_track("track", local_path=str(tmp_path / "track.opus"))
    partial_path = tmp_path / "track.partial.opus"
    partial_path.write_bytes(b"old")
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: True,
        refresh_recent_for_cache=lambda item: None,
        songs_dir=tmp_path,
        worker_count=0,
    )

    def fake_run(args, capture_output, text):
        partial_path.write_bytes(b"new")

        class Result:
            returncode = 1
            stderr = "download failed"

        return Result()

    monkeypatch.setattr("yt_bar.cache.subprocess.run", fake_run)

    assert manager._download_track_cache(track) is False
    assert not partial_path.exists()
    assert not tmp_path.joinpath("track.opus").exists()


def test_download_track_cache_creates_parent_directory_for_playlist_track(monkeypatch, tmp_path):
    playlist_dir = tmp_path / "mix"
    monkeypatch.setattr(
        "yt_bar.models.partial_cache_abspath_for_path",
        lambda local_path: str(playlist_dir / "track.partial.opus"),
    )
    track = make_track("track", local_path=str(playlist_dir / "track.opus"))
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: True,
        refresh_recent_for_cache=lambda item: None,
        songs_dir=tmp_path,
        worker_count=0,
    )

    def fake_run(args, capture_output, text):
        assert capture_output is True
        assert text is True
        assert playlist_dir.is_dir()
        playlist_dir.joinpath("track.partial.opus").write_bytes(b"audio")

        class Result:
            returncode = 0
            stderr = ""

        return Result()

    monkeypatch.setattr("yt_bar.cache.subprocess.run", fake_run)

    assert manager._download_track_cache(track) is True
    assert playlist_dir.joinpath("track.opus").read_bytes() == b"audio"


def test_cleanup_partial_cache_files_removes_nested_playlist_partials(tmp_path):
    nested = tmp_path / "mix"
    nested.mkdir()
    partial = nested / "track.partial.opus"
    partial.write_bytes(b"partial")
    manager = CacheManager(
        is_current_stream_item_active=lambda item, generation: True,
        refresh_recent_for_cache=lambda item: None,
        songs_dir=tmp_path,
        worker_count=0,
    )

    manager.cleanup_partial_cache_files()

    assert not partial.exists()
