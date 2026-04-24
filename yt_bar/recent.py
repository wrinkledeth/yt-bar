import threading
import time

from .models import MenuRecentEntry, RecentItem, ResolvedItem
from .storage import RecentStore


class RecentController:
    def __init__(self, store=None, clock=time.time):
        self._store = store or RecentStore()
        self._clock = clock
        self._lock = threading.RLock()
        self._entries: dict[str, RecentItem] = {}
        self._item_last_played: dict[str, float] = {}
        self._dirty = False

    def load(self):
        with self._lock:
            self._entries = self._store.load()
            self._dirty = False

    def sweep_stale_entries(self):
        with self._lock:
            changed = self._store.sweep_stale_entries(self._entries)
            if changed:
                self._save_locked()
                self._mark_dirty_locked()
            return changed

    def menu_entries(self, limit):
        with self._lock:
            entries = sorted(
                self._entries.values(),
                key=lambda entry: entry.last_played,
                reverse=True,
            )
            return tuple(
                MenuRecentEntry(cache_key=entry.cache_key, title=entry.display_title)
                for entry in entries[:limit]
            )

    def display_title_for_recent(self, cache_key):
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            return entry.display_title

    def rename(self, cache_key, title_override):
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                return False

            override = None
            if title_override is not None:
                override = str(title_override).strip() or None

            if entry.title_override == override:
                return False

            entry.title_override = override
            self._save_locked()
            self._mark_dirty_locked()
            return True

    def remove(self, cache_key):
        with self._lock:
            if cache_key not in self._entries:
                return False
            del self._entries[cache_key]
            self._save_locked()
            self._mark_dirty_locked()
            return True

    def consume_dirty(self):
        with self._lock:
            dirty = self._dirty
            self._dirty = False
            return dirty

    def record_item_played(self, item, *, last_played=None):
        timestamp = self._clock() if last_played is None else last_played
        with self._lock:
            self._item_last_played[item.cache_key] = timestamp
            return self._refresh_from_item_locked(
                item,
                last_played=timestamp,
                remove_if_empty=False,
            )

    def refresh_for_cache(self, item):
        with self._lock:
            return self._refresh_from_item_locked(
                item,
                last_played=self._latest_last_played_locked(item.cache_key),
                remove_if_empty=False,
            )

    def item_for_recent(self, cache_key):
        with self._lock:
            entry = self._entries.get(cache_key)
            if entry is None:
                return None
            return self._entry_to_item_locked(entry)

    def _save_locked(self):
        self._store.save(self._entries)

    def _mark_dirty_locked(self):
        self._dirty = True

    def _latest_last_played_locked(self, item_key):
        return self._item_last_played.get(item_key, self._clock())

    def _refresh_from_item_locked(
        self,
        item,
        *,
        last_played=None,
        remove_if_empty=False,
    ):
        cached_tracks = item.cached_tracks()
        item_key = item.cache_key
        existing = self._entries.get(item_key)
        effective_last_played = (
            last_played
            if last_played is not None
            else (
                existing.last_played
                if existing is not None
                else self._latest_last_played_locked(item_key)
            )
        )

        if item.kind == "video" and cached_tracks:
            cached_tracks = [cached_tracks[0]]

        if not cached_tracks:
            if remove_if_empty and existing is not None:
                del self._entries[item_key]
                self._save_locked()
                self._mark_dirty_locked()
                return True
            return False

        updated = RecentItem(
            kind=item.kind,
            id=item.id,
            title=item.title,
            source_url=item.source_url,
            last_played=effective_last_played,
            tracks=cached_tracks,
            title_override=existing.title_override if existing is not None else None,
        )

        if existing is not None and existing.to_dict() == updated.to_dict():
            return False

        self._entries[item_key] = updated
        self._save_locked()
        self._mark_dirty_locked()
        return True

    def _entry_to_item_locked(self, entry):
        valid_tracks = [track for track in entry.tracks if track.is_cached()]
        if not valid_tracks:
            del self._entries[entry.cache_key]
            self._save_locked()
            self._mark_dirty_locked()
            return None

        if len(valid_tracks) != len(entry.tracks):
            entry.tracks = valid_tracks
            self._save_locked()
            self._mark_dirty_locked()

        return ResolvedItem(
            kind=entry.kind,
            id=entry.id,
            title=entry.title,
            source_url=entry.source_url,
            tracks=list(valid_tracks),
        )
