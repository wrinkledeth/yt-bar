import os
import queue as queue_lib
import subprocess
import threading
from dataclasses import dataclass, field
from enum import Enum

from .constants import DECODER_QUEUE_BUFFERS
from .utils import (
    absolute_repo_path,
    cache_relpath_for_id,
    parse_duration,
    partial_cache_abspath_for_id,
    sanitize_cache_key,
    stable_hash,
)


@dataclass
class PlayRequest:
    url: str
    duration: float
    is_local: bool = False
    start_time: float = 0.0
    paused: bool = False
    on_finished: object = None
    on_stopped: object = None
    retry_kind: str | None = None
    retry_attempt: int = 0


class UICommandKind(Enum):
    PLAY = "play"
    STOPPED = "stopped"
    PAUSE = "pause"
    TOGGLE = "toggle"
    SEEK_DELTA = "seek_delta"


@dataclass(frozen=True)
class UICommand:
    kind: UICommandKind
    delta_seconds: float = 0.0

    @classmethod
    def play(cls):
        return cls(UICommandKind.PLAY)

    @classmethod
    def stopped(cls):
        return cls(UICommandKind.STOPPED)

    @classmethod
    def pause(cls):
        return cls(UICommandKind.PAUSE)

    @classmethod
    def toggle(cls):
        return cls(UICommandKind.TOGGLE)

    @classmethod
    def seek_delta(cls, delta_seconds):
        return cls(UICommandKind.SEEK_DELTA, float(delta_seconds))


@dataclass
class PlaybackGraphState:
    engine: object | None = None
    player: object | None = None
    mixer: object | None = None
    format: object | None = None
    notification_observer: object | None = None
    tap_block: object | None = None


@dataclass
class PlaybackDecoderState:
    stop_event: threading.Event | None = None
    queue: queue_lib.Queue = field(
        default_factory=lambda: queue_lib.Queue(maxsize=DECODER_QUEUE_BUFFERS)
    )
    thread: threading.Thread | None = None
    ytdlp_process: subprocess.Popen | None = None
    ffmpeg_process: subprocess.Popen | None = None
    generation: int = 0
    eof: bool = False
    failed: bool = False
    error: str | None = None


@dataclass
class PlaybackScheduleState:
    buffers: dict = field(default_factory=dict)
    frames_total: int = 0
    next_buffer_id: int = 0
    started_playback: bool = False
    last_rendered_frames: int = 0
    last_elapsed_seconds: float = 0.0


@dataclass
class PlaybackRouteState:
    rebuild_pending: bool = False
    rebuild_deadline: float = 0.0


@dataclass
class SeekTraceState:
    id: int = 0
    started_at: float = 0.0
    target: float = 0.0
    first_chunk_logged: bool = False
    first_buffer_logged: bool = False
    player_play_logged: bool = False
    elapsed_logged: bool = False


@dataclass
class PlaybackSession:
    id: int
    request: PlayRequest
    stop_event: threading.Event = field(default_factory=threading.Event)
    graph: PlaybackGraphState = field(default_factory=PlaybackGraphState)
    decoder: PlaybackDecoderState = field(default_factory=PlaybackDecoderState)
    schedule: PlaybackScheduleState = field(default_factory=PlaybackScheduleState)
    route: PlaybackRouteState = field(default_factory=PlaybackRouteState)
    seek_trace: SeekTraceState = field(default_factory=SeekTraceState)

    @property
    def duration(self):
        return self.request.duration

    @property
    def url(self):
        return self.request.url

    @property
    def is_local(self):
        return self.request.is_local

    @property
    def paused(self):
        return self.request.paused

    @paused.setter
    def paused(self, value):
        self.request.paused = value

    @property
    def base_offset_seconds(self):
        return self.request.start_time

    @base_offset_seconds.setter
    def base_offset_seconds(self, value):
        self.request.start_time = value

    @property
    def on_finished(self):
        return self.request.on_finished

    @property
    def on_stopped(self):
        return self.request.on_stopped


@dataclass
class TrackInfo:
    id: str
    title: str
    duration: float
    source_url: str
    local_path: str

    @property
    def absolute_local_path(self):
        return absolute_repo_path(self.local_path)

    @property
    def partial_local_path(self):
        return partial_cache_abspath_for_id(self.id)

    def is_cached(self):
        return os.path.exists(self.absolute_local_path)

    def to_dict(self):
        return {
            "id": self.id,
            "title": self.title,
            "duration": self.duration,
            "source_url": self.source_url,
            "local_path": self.local_path,
        }

    @classmethod
    def from_dict(cls, data):
        track_id = sanitize_cache_key(
            str(data.get("id") or stable_hash(data.get("source_url", "")))
        )
        return cls(
            id=track_id,
            title=(data.get("title") or "Unknown").strip() or "Unknown",
            duration=parse_duration(data.get("duration")),
            source_url=(data.get("source_url") or "").strip(),
            local_path=(data.get("local_path") or cache_relpath_for_id(track_id)),
        )


@dataclass
class ResolvedItem:
    kind: str
    id: str
    title: str
    source_url: str
    tracks: list[TrackInfo]

    @property
    def cache_key(self):
        return f"{self.kind}:{self.id}"

    def is_fully_cached(self):
        return bool(self.tracks) and all(track.is_cached() for track in self.tracks)

    def cached_tracks(self):
        return [track for track in self.tracks if track.is_cached()]


@dataclass
class RecentItem:
    kind: str
    id: str
    title: str
    source_url: str
    last_played: float
    tracks: list[TrackInfo]

    @property
    def cache_key(self):
        return f"{self.kind}:{self.id}"

    def to_dict(self):
        return {
            "kind": self.kind,
            "id": self.id,
            "title": self.title,
            "source_url": self.source_url,
            "last_played": self.last_played,
            "tracks": [track.to_dict() for track in self.tracks],
        }

    @classmethod
    def from_dict(cls, data):
        return cls(
            kind=(data.get("kind") or "video").strip() or "video",
            id=sanitize_cache_key(str(data.get("id") or stable_hash(data.get("source_url", "")))),
            title=(data.get("title") or "Unknown").strip() or "Unknown",
            source_url=(data.get("source_url") or "").strip(),
            last_played=float(data.get("last_played") or 0.0),
            tracks=[
                TrackInfo.from_dict(track_data)
                for track_data in data.get("tracks", [])
                if isinstance(track_data, dict)
            ],
        )


@dataclass
class CacheJob:
    item: ResolvedItem
    track: TrackInfo
