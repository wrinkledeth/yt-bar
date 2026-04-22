from yt_bar.models import RecentItem, ResolvedItem, TrackInfo
from yt_bar.recent import RecentController
from yt_bar.storage import RecentStore


class MemoryRecentStore:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.saved = []

    def load(self):
        return dict(self.entries)

    def save(self, entries):
        self.entries = dict(entries)
        self.saved.append(dict(entries))

    def sweep_stale_entries(self, entries):
        return RecentStore.sweep_stale_entries(entries)


def make_track(track_id, path, *, source_url=None):
    return TrackInfo(
        id=track_id,
        title=track_id.title(),
        duration=1.0,
        source_url=source_url if source_url is not None else f"https://example.test/{track_id}",
        local_path=str(path),
    )


def make_item(kind, item_id, tracks):
    return ResolvedItem(
        kind=kind,
        id=item_id,
        title=item_id.title(),
        source_url=f"https://example.test/{item_id}",
        tracks=list(tracks),
    )


def test_record_item_played_stores_cached_video_as_single_recent_entry(tmp_path):
    first_path = tmp_path / "first.opus"
    second_path = tmp_path / "second.opus"
    first_path.write_bytes(b"first")
    second_path.write_bytes(b"second")
    item = make_item(
        "video",
        "item",
        [
            make_track("first", first_path),
            make_track("second", second_path),
        ],
    )
    store = MemoryRecentStore()
    recent = RecentController(store=store, clock=lambda: 12.0)

    assert recent.record_item_played(item) is True

    saved = store.saved[-1][item.cache_key]
    assert saved.last_played == 12.0
    assert [track.id for track in saved.tracks] == ["first"]
    assert recent.menu_entries(10)[0].cache_key == item.cache_key
    assert recent.consume_dirty() is True
    assert recent.consume_dirty() is False


def test_refresh_for_cache_preserves_last_played_for_uncached_item(tmp_path):
    path = tmp_path / "track.opus"
    track = make_track("track", path)
    item = make_item("video", "item", [track])
    store = MemoryRecentStore()
    recent = RecentController(store=store, clock=lambda: 99.0)

    assert recent.record_item_played(item, last_played=25.0) is False
    assert store.saved == []

    path.write_bytes(b"cached")

    assert recent.refresh_for_cache(item) is True
    assert store.saved[-1][item.cache_key].last_played == 25.0


def test_item_for_recent_prunes_missing_tracks_and_returns_cached_subset(tmp_path):
    cached_path = tmp_path / "cached.opus"
    cached_path.write_bytes(b"cached")
    cached_track = make_track("cached", cached_path)
    missing_track = make_track("missing", tmp_path / "missing.opus")
    entry = RecentItem(
        kind="playlist",
        id="playlist",
        title="Playlist",
        source_url="https://example.test/playlist",
        last_played=40.0,
        tracks=[cached_track, missing_track],
    )
    store = MemoryRecentStore({entry.cache_key: entry})
    recent = RecentController(store=store)
    recent.load()

    item = recent.item_for_recent(entry.cache_key)

    assert item is not None
    assert item.kind == "playlist"
    assert [track.id for track in item.tracks] == ["cached"]
    assert [track.id for track in store.saved[-1][entry.cache_key].tracks] == ["cached"]
    assert recent.consume_dirty() is True


def test_item_for_recent_removes_entry_when_all_tracks_are_missing(tmp_path):
    track = make_track("missing", tmp_path / "missing.opus")
    entry = RecentItem(
        kind="video",
        id="missing",
        title="Missing",
        source_url="https://example.test/missing",
        last_played=40.0,
        tracks=[track],
    )
    store = MemoryRecentStore({entry.cache_key: entry})
    recent = RecentController(store=store)
    recent.load()

    assert recent.item_for_recent(entry.cache_key) is None
    assert entry.cache_key not in store.saved[-1]
    assert recent.menu_entries(10) == ()


def test_remove_saves_and_marks_dirty(tmp_path):
    path = tmp_path / "track.opus"
    path.write_bytes(b"track")
    entry = RecentItem(
        kind="video",
        id="track",
        title="Track",
        source_url="https://example.test/track",
        last_played=40.0,
        tracks=[make_track("track", path)],
    )
    store = MemoryRecentStore({entry.cache_key: entry})
    recent = RecentController(store=store)
    recent.load()

    assert recent.remove(entry.cache_key) is True
    assert entry.cache_key not in store.saved[-1]
    assert recent.remove(entry.cache_key) is False
    assert recent.consume_dirty() is True
