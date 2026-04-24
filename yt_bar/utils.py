import hashlib
import os
import traceback

import AppKit
import Foundation

from .constants import (
    APP_ROOT,
    PARTIAL_CACHE_SUFFIX,
    RECENT_TITLE_LIMIT,
    SAFE_CACHE_KEY_RE,
    SONGS_DIR,
    SONGS_DIR_NAME,
)

MANAGED_MEDIA_HASH_LENGTH = 8
MANAGED_MEDIA_TITLE_LIMIT = 48


def log_exception(context, exc):
    print(f"{context}: {exc!r}")
    print(f"{context} args: {getattr(exc, 'args', ())!r}")
    traceback.print_exc()


def stable_hash(value):
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def sanitize_cache_key(value):
    cleaned = SAFE_CACHE_KEY_RE.sub("_", value or "").strip("._")
    return cleaned or stable_hash(value or "track")


def cache_relpath_for_id(item_id, title=None):
    return _managed_media_relpath_for_id(item_id, title)


def playlist_cache_dir_relpath(playlist_id, title):
    directory_name = _managed_media_directory_name(playlist_id, title)
    readable_relpath = os.path.join(SONGS_DIR_NAME, directory_name)
    if os.path.isdir(absolute_repo_path(readable_relpath)):
        return readable_relpath

    existing_relpath = _existing_playlist_cache_dir_relpath(playlist_id)
    if existing_relpath is not None:
        return existing_relpath

    return readable_relpath


def playlist_track_relpath_for_id(playlist_id, playlist_title, track_id, track_title):
    playlist_relpath = _managed_media_relpath_for_id(
        track_id,
        track_title,
        directory=playlist_cache_dir_relpath(playlist_id, playlist_title),
    )
    if os.path.exists(absolute_repo_path(playlist_relpath)):
        return playlist_relpath

    existing_root_relpath = _existing_root_cache_relpath_for_id(track_id)
    if existing_root_relpath is not None:
        return existing_root_relpath

    return playlist_relpath


def absolute_repo_path(path):
    if os.path.isabs(path):
        return path
    return os.path.join(APP_ROOT, path)


def partial_cache_abspath_for_id(item_id):
    return partial_cache_abspath_for_path(_legacy_cache_relpath_for_id(item_id))


def partial_cache_abspath_for_path(local_path):
    final_path = absolute_repo_path(local_path)
    stem, _ = os.path.splitext(final_path)
    return f"{stem}{PARTIAL_CACHE_SUFFIX}"


def truncate_title(title, limit=RECENT_TITLE_LIMIT):
    text = (title or "").strip() or "Unknown"
    if len(text) <= limit:
        return text
    if limit <= 1:
        return text[:limit]
    return f"{text[: limit - 1]}…"


def format_time(seconds):
    total = max(0, int(seconds or 0))
    mins, secs = divmod(total, 60)
    hours, mins = divmod(mins, 60)
    if hours:
        return f"{hours}:{mins:02d}:{secs:02d}"
    return f"{mins}:{secs:02d}"


def parse_duration(value):
    try:
        duration = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, duration)


def _legacy_cache_relpath_for_id(item_id):
    return os.path.join(SONGS_DIR_NAME, f"{sanitize_cache_key(item_id)}.opus")


def _managed_media_filename(item_id, title):
    return (
        f"{_managed_media_slug(title)}-{stable_hash(str(item_id))[:MANAGED_MEDIA_HASH_LENGTH]}.opus"
    )


def _managed_media_directory_name(item_id, title):
    return f"{_managed_media_slug(title)}-{stable_hash(str(item_id))[:MANAGED_MEDIA_HASH_LENGTH]}"


def _managed_media_slug(title):
    cleaned = SAFE_CACHE_KEY_RE.sub("_", (title or "").strip()).strip("._")
    if len(cleaned) > MANAGED_MEDIA_TITLE_LIMIT:
        cleaned = cleaned[:MANAGED_MEDIA_TITLE_LIMIT].rstrip("._-")
    return cleaned or "Unknown"


def _managed_media_relpath_for_id(item_id, title=None, directory=SONGS_DIR_NAME):
    legacy_relpath = _legacy_cache_relpath_for_id(item_id)
    if title is None:
        if directory == SONGS_DIR_NAME:
            return legacy_relpath
        return os.path.join(directory, f"{sanitize_cache_key(item_id)}.opus")

    if directory == SONGS_DIR_NAME and os.path.exists(absolute_repo_path(legacy_relpath)):
        return legacy_relpath

    readable_relpath = os.path.join(
        directory,
        _managed_media_filename(item_id, title),
    )
    if os.path.exists(absolute_repo_path(readable_relpath)):
        return readable_relpath

    existing_relpath = _existing_readable_cache_relpath_for_id(item_id, directory=directory)
    if existing_relpath is not None:
        return existing_relpath

    return readable_relpath


def _existing_readable_cache_relpath_for_id(item_id, directory=SONGS_DIR_NAME):
    suffix = f"-{stable_hash(str(item_id))[:MANAGED_MEDIA_HASH_LENGTH]}.opus"
    try:
        names = sorted(os.listdir(absolute_repo_path(directory)))
    except OSError:
        return None

    for name in names:
        if name.endswith(suffix):
            path = os.path.join(directory, name)
            if os.path.isfile(absolute_repo_path(path)):
                return path
    return None


def _existing_root_cache_relpath_for_id(item_id):
    legacy_relpath = _legacy_cache_relpath_for_id(item_id)
    if os.path.exists(absolute_repo_path(legacy_relpath)):
        return legacy_relpath
    return _existing_readable_cache_relpath_for_id(item_id)


def _existing_playlist_cache_dir_relpath(playlist_id):
    suffix = f"-{stable_hash(str(playlist_id))[:MANAGED_MEDIA_HASH_LENGTH]}"
    try:
        names = sorted(os.listdir(SONGS_DIR))
    except OSError:
        return None

    for name in names:
        path = os.path.join(SONGS_DIR, name)
        if name.endswith(suffix) and os.path.isdir(path):
            return os.path.join(SONGS_DIR_NAME, name)
    return None


def _set_header_title(menu_item, primary, trailing=None):
    """Render a header menu-item at secondaryLabelColor brightness."""
    text = primary + ("  " + trailing if trailing else "")
    s = Foundation.NSMutableAttributedString.alloc().initWithString_(text)
    s.addAttribute_value_range_(
        AppKit.NSForegroundColorAttributeName,
        AppKit.NSColor.secondaryLabelColor(),
        Foundation.NSRange(0, s.length()),
    )
    menu_item._menuitem.setAttributedTitle_(s)


def progress_bar(elapsed, duration, width=20):
    if duration is None or duration <= 0:
        return f"{format_time(elapsed)}"
    clamped = max(0, min(elapsed, duration))
    if width <= 0:
        return f"{format_time(clamped)} / {format_time(duration)}"
    frac = clamped / duration
    playhead_pos = round(frac * (width - 1))
    bar = "━" * playhead_pos + "●" + "─" * (width - 1 - playhead_pos)
    return f"{bar}  {format_time(clamped)} / {format_time(duration)}"
