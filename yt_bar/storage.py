import json
import os
from dataclasses import dataclass

from .constants import (
    DEFAULT_RECENT_MENU_LIMIT,
    DEFAULT_SKIP_INTERVAL_SECONDS,
    RECENT_INDEX_PATH,
    RECENT_SIZE_PRESETS,
    SETTINGS_PATH,
    SKIP_INTERVAL_PRESETS,
)
from .models import RecentItem
from .utils import log_exception


@dataclass
class Settings:
    skip_interval_seconds: float = DEFAULT_SKIP_INTERVAL_SECONDS
    recent_menu_limit: int = DEFAULT_RECENT_MENU_LIMIT
    show_play_pause: bool = True
    show_seek: bool = True
    show_songs: bool = True


class SettingsStore:
    def __init__(self, path=SETTINGS_PATH):
        self.path = path

    def load(self):
        settings = Settings()
        if not os.path.exists(self.path):
            return settings

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log_exception("Failed to load settings", exc)
            return settings

        if not isinstance(payload, dict):
            return settings

        skip = payload.get("skip_interval_seconds")
        if isinstance(skip, (int, float)) and skip in SKIP_INTERVAL_PRESETS:
            settings.skip_interval_seconds = float(skip)

        limit = payload.get("recent_menu_limit")
        if isinstance(limit, int) and limit in RECENT_SIZE_PRESETS:
            settings.recent_menu_limit = limit

        compact = payload.get("compact_menu")
        legacy_visibility = None
        if isinstance(compact, bool):
            legacy_visibility = not compact

        show_play_pause = payload.get("show_play_pause")
        if isinstance(show_play_pause, bool):
            settings.show_play_pause = show_play_pause
        elif legacy_visibility is not None:
            settings.show_play_pause = legacy_visibility

        show_seek = payload.get("show_seek")
        if isinstance(show_seek, bool):
            settings.show_seek = show_seek
        elif legacy_visibility is not None:
            settings.show_seek = legacy_visibility

        show_songs = payload.get("show_songs")
        if isinstance(show_songs, bool):
            settings.show_songs = show_songs
        elif legacy_visibility is not None:
            settings.show_songs = legacy_visibility

        return settings

    def save(self, settings):
        payload = {
            "skip_interval_seconds": settings.skip_interval_seconds,
            "recent_menu_limit": settings.recent_menu_limit,
            "show_play_pause": settings.show_play_pause,
            "show_seek": settings.show_seek,
            "show_songs": settings.show_songs,
        }
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
        os.replace(tmp_path, self.path)


class RecentStore:
    def __init__(self, path=RECENT_INDEX_PATH):
        self.path = path

    def load(self):
        if not os.path.exists(self.path):
            return {}

        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, json.JSONDecodeError) as exc:
            log_exception("Failed to load recent index", exc)
            return {}

        if not isinstance(payload, list):
            return {}

        entries = {}
        for item in payload:
            if not isinstance(item, dict):
                continue
            recent = RecentItem.from_dict(item)
            if recent.tracks:
                entries[recent.cache_key] = recent
        return entries

    def save(self, entries):
        payload = [
            entry.to_dict()
            for entry in sorted(
                entries.values(),
                key=lambda entry: entry.last_played,
                reverse=True,
            )
        ]
        tmp_path = f"{self.path}.tmp"
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, ensure_ascii=True)
        os.replace(tmp_path, self.path)

    @staticmethod
    def sweep_stale_entries(entries):
        changed = False
        for key, entry in list(entries.items()):
            valid_tracks = [track for track in entry.tracks if track.is_cached()]
            if not valid_tracks:
                del entries[key]
                changed = True
                continue
            if len(valid_tracks) != len(entry.tracks):
                entry.tracks = valid_tracks
                changed = True
        return changed
