import os
import re
import subprocess

import AVFoundation
import Foundation

from .models import ResolvedItem, TrackInfo
from .utils import absolute_repo_path, cache_relpath_for_id, log_exception, stable_hash

FFMPEG_DURATION_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")


def import_local_file(source_path):
    source_path = os.path.abspath(os.path.expanduser(str(source_path or "").strip()))
    if not source_path or not os.path.isfile(source_path):
        return None

    title = probe_local_title(source_path) or fallback_local_title(source_path)
    item_id = local_item_id_for_path(source_path)
    imported_relpath = imported_local_relpath_for_path(source_path, title)
    imported_path = absolute_repo_path(imported_relpath)

    if not _transcode_into_library(source_path, imported_path):
        return None

    duration = probe_local_duration(source_path)
    track = TrackInfo(
        id=item_id,
        title=title,
        duration=duration,
        source_url=source_path,
        local_path=imported_relpath,
    )
    return ResolvedItem(
        kind="local",
        id=item_id,
        title=title,
        source_url=source_path,
        tracks=[track],
    )


def local_item_id_for_path(source_path):
    return f"local_{stable_hash(os.path.abspath(source_path))}"


def imported_local_relpath_for_path(source_path, title):
    item_id = local_item_id_for_path(source_path)
    return cache_relpath_for_id(item_id, title)


def imported_local_abspath_for_path(source_path, title):
    return absolute_repo_path(imported_local_relpath_for_path(source_path, title))


def fallback_local_title(source_path):
    title = os.path.splitext(os.path.basename(source_path))[0].strip()
    return title or "Unknown"


def probe_local_title(source_path):
    try:
        url = Foundation.NSURL.fileURLWithPath_(os.path.abspath(source_path))
        asset = AVFoundation.AVURLAsset.URLAssetWithURL_options_(url, None)
        metadata_items = asset.commonMetadata() or ()
    except Exception:
        return ""

    for item in metadata_items:
        try:
            if item.commonKey() != AVFoundation.AVMetadataCommonKeyTitle:
                continue
            value = item.stringValue() or item.value()
            title = str(value or "").strip()
            if title:
                return title
        except Exception:
            continue
    return ""


def probe_local_duration(source_path):
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-i",
                source_path,
                "-t",
                "0",
                "-f",
                "null",
                "-",
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return 0.0

    output = "\n".join(part for part in (result.stdout, result.stderr) if part)
    match = FFMPEG_DURATION_RE.search(output)
    if match is None:
        return 0.0

    hours, minutes, seconds = match.groups()
    return int(hours) * 3600 + int(minutes) * 60 + float(seconds)


def _transcode_into_library(source_path, imported_path):
    os.makedirs(os.path.dirname(imported_path), exist_ok=True)
    tmp_path = f"{os.path.splitext(imported_path)[0]}.tmp.opus"
    try:
        result = subprocess.run(
            [
                "ffmpeg",
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-i",
                source_path,
                "-vn",
                "-c:a",
                "libopus",
                tmp_path,
            ],
            capture_output=True,
            check=False,
            text=True,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        log_exception("Failed to import local file", exc)
        return False

    if result.returncode != 0:
        error_text = result.stderr.strip() or result.stdout.strip() or "unknown ffmpeg failure"
        print(f"local import failed for {source_path}: {error_text}")
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return False

    try:
        os.replace(tmp_path, imported_path)
        return True
    except OSError as exc:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        log_exception("Failed to import local file", exc)
        return False
