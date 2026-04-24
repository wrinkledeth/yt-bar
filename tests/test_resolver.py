from yt_bar import resolver
from yt_bar.utils import cache_relpath_for_id


def test_default_source_url_prefers_http_metadata_url():
    info = {
        "webpage_url": "https://example.test/watch",
        "original_url": "https://example.test/original",
        "url": "https://media.example.test/stream",
    }

    assert (
        resolver.default_source_url(info, "https://fallback.test") == "https://example.test/watch"
    )


def test_default_source_url_rebuilds_youtube_watch_url_from_id():
    info = {"id": "abc123", "extractor_key": "Youtube"}

    assert resolver.default_source_url(info, "") == "https://www.youtube.com/watch?v=abc123"


def test_track_from_info_sanitizes_id_and_parses_duration(monkeypatch, tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()
    monkeypatch.setattr("yt_bar.utils.APP_ROOT", str(tmp_path))
    monkeypatch.setattr("yt_bar.utils.SONGS_DIR", str(songs_dir))

    track = resolver.track_from_info(
        {
            "id": "artist / song?",
            "title": "  Example Title  ",
            "duration": "123.5",
            "webpage_url": "https://example.test/video",
        },
        "https://fallback.test",
    )

    assert track is not None
    assert track.id == "artist_song"
    assert track.title == "Example Title"
    assert track.duration == 123.5
    assert track.source_url == "https://example.test/video"
    assert track.local_path == cache_relpath_for_id("artist_song", "Example Title")


def test_resolve_playlist_builds_tracks_and_skips_invalid_entries(monkeypatch):
    def fake_run(args, timeout):
        assert args[:4] == ["yt-dlp", "-J", "--flat-playlist", "--no-warnings"]
        assert timeout == 60
        return {
            "id": "playlist-1",
            "title": "  Mix  ",
            "webpage_url": "https://example.test/playlist",
            "entries": [
                {
                    "id": "one",
                    "title": "First",
                    "duration": 10,
                    "url": "https://example.test/one",
                },
                "not a dict",
                {
                    "id": "two",
                    "title": "",
                    "duration": None,
                    "url": "https://example.test/two",
                },
            ],
        }

    monkeypatch.setattr(resolver, "run_yt_dlp_json", fake_run)

    item = resolver.resolve_playlist("https://example.test/playlist?list=abc")

    assert item is not None
    assert item.kind == "playlist"
    assert item.id == "playlist-1"
    assert item.title == "Mix"
    assert item.source_url == "https://example.test/playlist"
    assert [track.id for track in item.tracks] == ["one", "two"]
    assert item.tracks[1].title == "Track 3"


def test_resolve_url_falls_back_to_single_when_playlist_resolution_fails(monkeypatch):
    calls = []

    def fake_playlist(url):
        calls.append(("playlist", url))
        return None

    def fake_single(url):
        calls.append(("single", url))
        return "single item"

    monkeypatch.setattr(resolver, "resolve_playlist", fake_playlist)
    monkeypatch.setattr(resolver, "resolve_single", fake_single)

    assert resolver.resolve_url("https://example.test/watch?v=1&list=abc") == "single item"
    assert calls == [
        ("playlist", "https://example.test/watch?v=1&list=abc"),
        ("single", "https://example.test/watch?v=1&list=abc"),
    ]
