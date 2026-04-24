import json
import os

from yt_bar.models import RecentItem, TrackInfo
from yt_bar.storage import RecentStore, Settings, SettingsStore


def test_settings_store_loads_defaults_for_missing_and_invalid_values(tmp_path):
    missing_store = SettingsStore(tmp_path / "missing.json")
    assert missing_store.load() == Settings()

    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "skip_interval_seconds": 17,
                "recent_menu_limit": 999,
                "compact_menu": "yes",
            }
        ),
        encoding="utf-8",
    )

    assert SettingsStore(path).load() == Settings()


def test_settings_store_loads_legacy_compact_menu_as_hidden_transport_items(tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(
        json.dumps(
            {
                "compact_menu": True,
            }
        ),
        encoding="utf-8",
    )

    assert SettingsStore(path).load() == Settings(
        show_play_pause=False,
        show_seek=False,
        show_songs=False,
    )


def test_settings_store_round_trips_valid_settings(tmp_path):
    path = tmp_path / "settings.json"
    store = SettingsStore(path)
    settings = Settings(
        skip_interval_seconds=60.0,
        recent_menu_limit=20,
        show_play_pause=False,
        show_seek=True,
        show_songs=False,
    )

    store.save(settings)

    assert store.load() == settings
    assert not os.path.exists(f"{path}.tmp")
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload == {
        "skip_interval_seconds": 60.0,
        "recent_menu_limit": 20,
        "show_play_pause": False,
        "show_seek": True,
        "show_songs": False,
    }


def test_recent_store_loads_valid_entries_by_cache_key(tmp_path):
    path = tmp_path / "recent.json"
    path.write_text(
        json.dumps(
            [
                {
                    "kind": "video",
                    "id": "one",
                    "title": "One",
                    "source_url": "https://example.test/one",
                    "last_played": 2.0,
                    "tracks": [
                        {
                            "id": "track one",
                            "title": "Track One",
                            "duration": "12.5",
                            "source_url": "https://example.test/one",
                            "local_path": "songs/one.opus",
                        }
                    ],
                },
                {"kind": "video", "id": "empty", "tracks": []},
                "not a dict",
            ]
        ),
        encoding="utf-8",
    )

    entries = RecentStore(path).load()

    assert list(entries) == ["video:one"]
    recent = entries["video:one"]
    assert recent.title == "One"
    assert recent.title_override is None
    assert recent.tracks[0].id == "track_one"
    assert recent.tracks[0].duration == 12.5
    assert recent.tracks[0].local_path == "songs/one.opus"


def test_recent_store_round_trips_title_override(tmp_path):
    path = tmp_path / "recent.json"
    store = RecentStore(path)
    entry = RecentItem(
        kind="video",
        id="named",
        title="Original Title",
        source_url="https://example.test/named",
        last_played=4.0,
        tracks=[
            TrackInfo(
                id="named",
                title="Original Title",
                duration=1.0,
                source_url="https://example.test/named",
                local_path="songs/named.opus",
            )
        ],
        title_override="Custom Label",
    )

    store.save({entry.cache_key: entry})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload[0]["title_override"] == "Custom Label"
    assert store.load()[entry.cache_key].title_override == "Custom Label"


def test_recent_store_saves_newest_entries_first(tmp_path):
    path = tmp_path / "recent.json"
    store = RecentStore(path)
    older = RecentItem(
        kind="video",
        id="older",
        title="Older",
        source_url="https://example.test/older",
        last_played=1.0,
        tracks=[
            TrackInfo(
                id="older",
                title="Older",
                duration=1.0,
                source_url="https://example.test/older",
                local_path="songs/older.opus",
            )
        ],
    )
    newer = RecentItem(
        kind="video",
        id="newer",
        title="Newer",
        source_url="https://example.test/newer",
        last_played=3.0,
        tracks=[
            TrackInfo(
                id="newer",
                title="Newer",
                duration=1.0,
                source_url="https://example.test/newer",
                local_path="songs/newer.opus",
            )
        ],
    )

    store.save({older.cache_key: older, newer.cache_key: newer})

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert [item["id"] for item in payload] == ["newer", "older"]
    assert not os.path.exists(f"{path}.tmp")


def test_recent_store_sweeps_stale_entries(tmp_path):
    cached_path = tmp_path / "cached.opus"
    cached_path.write_bytes(b"cached")
    cached_track = TrackInfo(
        id="cached",
        title="Cached",
        duration=1.0,
        source_url="https://example.test/cached",
        local_path=str(cached_path),
    )
    missing_track = TrackInfo(
        id="missing",
        title="Missing",
        duration=1.0,
        source_url="https://example.test/missing",
        local_path=str(tmp_path / "missing.opus"),
    )
    partial_entry = RecentItem(
        kind="playlist",
        id="partial",
        title="Partial",
        source_url="https://example.test/playlist",
        last_played=3.0,
        tracks=[cached_track, missing_track],
    )
    stale_entry = RecentItem(
        kind="video",
        id="stale",
        title="Stale",
        source_url="https://example.test/stale",
        last_played=2.0,
        tracks=[missing_track],
    )
    entries = {
        partial_entry.cache_key: partial_entry,
        stale_entry.cache_key: stale_entry,
    }

    changed = RecentStore.sweep_stale_entries(entries)

    assert changed is True
    assert list(entries) == ["playlist:partial"]
    assert entries["playlist:partial"].tracks == [cached_track]
