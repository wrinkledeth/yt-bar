import json
import subprocess

from .models import ResolvedItem, TrackInfo
from .utils import (
    cache_relpath_for_id,
    log_exception,
    parse_duration,
    sanitize_cache_key,
    stable_hash,
)


def default_source_url(info, fallback_url=""):
    for key in ("webpage_url", "original_url", "url"):
        value = info.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value

    video_id = info.get("id")
    extractor = info.get("extractor_key") or info.get("ie_key") or info.get("extractor") or ""
    lower_fallback = fallback_url.lower()
    if (
        isinstance(video_id, str)
        and video_id
        and (
            "youtube" in str(extractor).lower()
            or "youtube.com" in lower_fallback
            or "youtu.be" in lower_fallback
        )
    ):
        return f"https://www.youtube.com/watch?v={video_id}"

    return fallback_url


def is_playlist_url(url):
    return "list=" in url or "/playlist" in url


def run_yt_dlp_json(args, timeout):
    try:
        result = subprocess.run(
            args,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        print(f"yt-dlp timed out while resolving {args[-1]}")
        return None

    if result.returncode != 0:
        error_text = result.stderr.strip() or "unknown yt-dlp failure"
        print(f"yt-dlp failed for {args[-1]}: {error_text}")
        return None

    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        log_exception("yt-dlp JSON parse failure", exc)
        return None


def track_from_info(info, fallback_url, default_title="Unknown"):
    source_url = default_source_url(info, fallback_url).strip()
    if not source_url:
        return None

    raw_id = str(info.get("id") or stable_hash(source_url))
    track_id = sanitize_cache_key(raw_id)
    return TrackInfo(
        id=track_id,
        title=(info.get("title") or default_title).strip() or default_title,
        duration=parse_duration(info.get("duration")),
        source_url=source_url,
        local_path=cache_relpath_for_id(track_id),
    )


def resolve_playlist(url):
    info = run_yt_dlp_json(
        ["yt-dlp", "-J", "--flat-playlist", "--no-warnings", url],
        timeout=60,
    )
    if not isinstance(info, dict):
        return None

    tracks = []
    for index, entry in enumerate(info.get("entries") or []):
        if not isinstance(entry, dict):
            continue
        track = track_from_info(
            entry,
            url,
            default_title=f"Track {index + 1}",
        )
        if track is None:
            continue
        tracks.append(track)

    if not tracks:
        print(f"yt-dlp returned no playlist entries for {url}")
        return None

    playlist_id = sanitize_cache_key(str(info.get("id") or stable_hash(url)))
    return ResolvedItem(
        kind="playlist",
        id=playlist_id,
        title=(info.get("title") or "Playlist").strip() or "Playlist",
        source_url=default_source_url(info, url) or url,
        tracks=tracks,
    )


def resolve_single(url):
    info = run_yt_dlp_json(
        ["yt-dlp", "-J", "--no-playlist", "--no-warnings", url],
        timeout=30,
    )
    if not isinstance(info, dict):
        return None

    track = track_from_info(info, url)
    if track is None:
        return None

    return ResolvedItem(
        kind="video",
        id=track.id,
        title=track.title,
        source_url=track.source_url,
        tracks=[track],
    )


def resolve_url(url):
    if is_playlist_url(url):
        item = resolve_playlist(url)
        if item is not None:
            return item
    return resolve_single(url)
