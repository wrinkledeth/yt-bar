import threading
from dataclasses import dataclass

from .models import ResolvedItem, TrackInfo

STREAM_PLAYBACK_MODE = "stream"
LOCAL_PLAYBACK_MODE = "local"


@dataclass(frozen=True)
class PlaybackStart:
    item: ResolvedItem
    playback_mode: str
    generation: int
    should_cache: bool


@dataclass(frozen=True)
class TrackPlayback:
    index: int
    track: TrackInfo
    playback_mode: str
    source: str
    is_local: bool


@dataclass(frozen=True)
class TrackAdvance:
    next_index: int | None

    @property
    def has_next_track(self):
        return self.next_index is not None


class PlaybackController:
    def __init__(self, *, lock=None):
        self._lock = lock or threading.RLock()
        self._tracks: list[TrackInfo] = []
        self._current_index = -1
        self._current_item: ResolvedItem | None = None
        self._current_playback_mode = STREAM_PLAYBACK_MODE
        self._current_item_generation = 0

    @staticmethod
    def playback_mode_for_item(item):
        if item.is_fully_cached():
            return LOCAL_PLAYBACK_MODE
        return STREAM_PLAYBACK_MODE

    def current_track(self):
        with self._lock:
            return self._track_at_current_index_locked()

    def current_track_snapshot(self):
        with self._lock:
            track = self._track_at_current_index_locked()
            if track is None:
                return -1, None
            return self._current_index, track

    def has_current_track(self):
        with self._lock:
            return self._track_at_current_index_locked() is not None

    def start_item(self, item, *, playback_mode):
        with self._lock:
            self._current_item = item
            self._tracks = list(item.tracks)
            self._current_index = 0
            self._current_playback_mode = playback_mode
            self._current_item_generation += 1
            generation = self._current_item_generation

        return PlaybackStart(
            item=item,
            playback_mode=playback_mode,
            generation=generation,
            should_cache=playback_mode == STREAM_PLAYBACK_MODE,
        )

    def select_track(self, index):
        with self._lock:
            if index < 0 or index >= len(self._tracks):
                return None

            track = self._tracks[index]
            playback_mode = self._current_playback_mode
            self._current_index = index

        if playback_mode == LOCAL_PLAYBACK_MODE and track.local_path:
            source = track.absolute_local_path
            is_local = True
        else:
            source = track.source_url
            is_local = False

        return TrackPlayback(
            index=index,
            track=track,
            playback_mode=playback_mode,
            source=source,
            is_local=is_local,
        )

    def advance_after_track_finished(self):
        with self._lock:
            next_index = self._current_index + 1
            if next_index >= len(self._tracks):
                return TrackAdvance(next_index=None)

            self._current_index = next_index
            return TrackAdvance(next_index=next_index)

    def next_track_index(self):
        with self._lock:
            next_index = self._current_index + 1
            if next_index >= len(self._tracks):
                return None
            return next_index

    def is_current_stream_item(self, item, generation):
        with self._lock:
            return (
                generation == self._current_item_generation
                and self._current_item is not None
                and self._current_item.cache_key == item.cache_key
                and self._current_playback_mode == STREAM_PLAYBACK_MODE
            )

    def _track_at_current_index_locked(self):
        if 0 <= self._current_index < len(self._tracks):
            return self._tracks[self._current_index]
        return None
