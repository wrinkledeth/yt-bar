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


def log_exception(context, exc):
    print(f"{context}: {exc!r}")
    print(f"{context} args: {getattr(exc, 'args', ())!r}")
    traceback.print_exc()


def stable_hash(value):
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:16]


def sanitize_cache_key(value):
    cleaned = SAFE_CACHE_KEY_RE.sub("_", value or "").strip("._")
    return cleaned or stable_hash(value or "track")


def cache_relpath_for_id(item_id):
    return os.path.join(SONGS_DIR_NAME, f"{sanitize_cache_key(item_id)}.opus")


def absolute_repo_path(path):
    if os.path.isabs(path):
        return path
    return os.path.join(APP_ROOT, path)


def partial_cache_abspath_for_id(item_id):
    return os.path.join(
        SONGS_DIR, f"{sanitize_cache_key(item_id)}{PARTIAL_CACHE_SUFFIX}"
    )


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
