import numpy as np

from yt_bar import visualizer
from yt_bar.utils import (
    cache_relpath_for_id,
    format_time,
    parse_duration,
    progress_bar,
    sanitize_cache_key,
    stable_hash,
    truncate_title,
)


def test_cache_key_helpers_are_stable_and_filesystem_safe():
    assert stable_hash("https://example.test/video") == stable_hash("https://example.test/video")
    assert len(stable_hash("https://example.test/video")) == 16
    assert sanitize_cache_key(" Artist / Song? ") == "Artist_Song"
    assert sanitize_cache_key("...") == stable_hash("...")
    assert cache_relpath_for_id(" Artist / Song? ") == "songs/Artist_Song.opus"


def test_cache_relpath_for_id_builds_readable_name_when_title_is_given(monkeypatch, tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()
    monkeypatch.setattr("yt_bar.utils.APP_ROOT", str(tmp_path))
    monkeypatch.setattr("yt_bar.utils.SONGS_DIR", str(songs_dir))

    assert (
        cache_relpath_for_id("track-id", " Artist / Song? ")
        == f"songs/Artist_Song-{stable_hash('track-id')[:8]}.opus"
    )


def test_cache_relpath_for_id_prefers_existing_legacy_file(monkeypatch, tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()
    monkeypatch.setattr("yt_bar.utils.APP_ROOT", str(tmp_path))
    monkeypatch.setattr("yt_bar.utils.SONGS_DIR", str(songs_dir))
    legacy_path = songs_dir / "track-id.opus"
    legacy_path.write_bytes(b"legacy")

    assert cache_relpath_for_id("track-id", "Readable Title") == "songs/track-id.opus"


def test_cache_relpath_for_id_reuses_existing_same_hash_file(monkeypatch, tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()
    monkeypatch.setattr("yt_bar.utils.APP_ROOT", str(tmp_path))
    monkeypatch.setattr("yt_bar.utils.SONGS_DIR", str(songs_dir))
    existing_name = f"Old_Title-{stable_hash('track-id')[:8]}.opus"
    (songs_dir / existing_name).write_bytes(b"cached")

    assert cache_relpath_for_id("track-id", "New Title") == f"songs/{existing_name}"


def test_duration_and_time_formatting_handles_bad_and_large_values():
    assert parse_duration("12.5") == 12.5
    assert parse_duration("-2") == 0.0
    assert parse_duration("not a number") == 0.0
    assert format_time(None) == "0:00"
    assert format_time(65.9) == "1:05"
    assert format_time(3661) == "1:01:01"


def test_truncate_title_uses_unknown_and_limit():
    assert truncate_title("") == "Unknown"
    assert truncate_title("Short", limit=10) == "Short"
    assert truncate_title("abcdef", limit=4) == "abc…"
    assert truncate_title("abcdef", limit=1) == "a"


def test_progress_bar_handles_unknown_duration_and_clamps_elapsed():
    assert progress_bar(65, None) == "1:05"
    assert progress_bar(200, 100, width=0) == "1:40 / 1:40"
    assert progress_bar(50, 100, width=5) == "━━●──  0:50 / 1:40"
    assert progress_bar(-10, 100, width=5) == "●────  0:00 / 1:40"


def test_grid_to_braille_maps_thresholded_grid_columns_to_characters():
    grid = np.zeros((6, 4), dtype=float)
    grid[0, 3] = 0.19
    grid[1, 0] = 0.18
    grid[2, 1] = 0.5
    grid[5, 2] = 1.0

    assert visualizer.grid_to_braille(grid) == "⠁⠄⠐"
