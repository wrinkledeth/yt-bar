from pathlib import Path
from types import SimpleNamespace

import yt_bar.local_media as local_media
from yt_bar.recent import RecentController


class MemoryRecentStore:
    def __init__(self, entries=None):
        self.entries = dict(entries or {})
        self.saved = []

    def load(self):
        return dict(self.entries)

    def save(self, entries):
        self.entries = dict(entries)
        self.saved.append(dict(entries))

    @staticmethod
    def sweep_stale_entries(entries):
        return False


def install_managed_media_root(monkeypatch, tmp_path):
    songs_dir = tmp_path / "songs"
    songs_dir.mkdir()
    monkeypatch.setattr("yt_bar.utils.APP_ROOT", str(tmp_path))
    monkeypatch.setattr("yt_bar.utils.SONGS_DIR", str(songs_dir))
    return songs_dir


def install_transcode_success(monkeypatch, payloads):
    payload_iter = iter(payloads)

    def fake_run(args, capture_output, check, text):
        assert capture_output is True
        assert check is False
        assert text is True
        assert args[:8] == [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            args[6],
            "-vn",
        ]
        assert args[8:] == ["-c:a", "libopus", args[-1]]
        Path(args[-1]).write_bytes(next(payload_iter))
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(local_media.subprocess, "run", fake_run)


def test_import_local_file_transcodes_source_and_builds_local_item(monkeypatch, tmp_path):
    source = tmp_path / "picked track.mp3"
    source.write_bytes(b"source-bytes")
    songs_dir = install_managed_media_root(monkeypatch, tmp_path)

    monkeypatch.setattr(local_media, "probe_local_title", lambda path: "Picked Track")
    monkeypatch.setattr(local_media, "probe_local_duration", lambda path: 123.5)
    install_transcode_success(monkeypatch, [b"normalized-bytes"])

    item = local_media.import_local_file(str(source))

    assert item is not None
    assert item.kind == "local"
    assert item.title == "Picked Track"
    assert item.source_url == str(source.resolve())
    assert item.tracks[0].local_path == local_media.imported_local_relpath_for_path(
        str(source),
        "Picked Track",
    )
    assert item.tracks[0].local_path.endswith(".opus")
    assert (
        songs_dir.joinpath(Path(item.tracks[0].local_path).name).read_bytes() == b"normalized-bytes"
    )


def test_import_local_file_reuses_stable_destination_for_same_source(monkeypatch, tmp_path):
    source = tmp_path / "picked track.mp3"
    source.write_bytes(b"first")
    songs_dir = install_managed_media_root(monkeypatch, tmp_path)
    titles = iter(["First Title", "Second Title"])

    monkeypatch.setattr(local_media, "probe_local_title", lambda path: next(titles))
    monkeypatch.setattr(local_media, "probe_local_duration", lambda path: 123.5)
    install_transcode_success(monkeypatch, [b"first-normalized", b"second-normalized"])

    first = local_media.import_local_file(str(source))
    source.write_bytes(b"second")
    second = local_media.import_local_file(str(source))

    assert first is not None
    assert second is not None
    assert first.cache_key == second.cache_key
    assert first.tracks[0].local_path == second.tracks[0].local_path
    assert first.tracks[0].local_path == local_media.imported_local_relpath_for_path(
        str(source),
        "Second Title",
    )
    assert (
        songs_dir.joinpath(Path(first.tracks[0].local_path).name).read_bytes()
        == b"second-normalized"
    )


def test_probe_local_duration_parses_ffmpeg_output(monkeypatch):
    def fake_run(*_args, **_kwargs):
        return SimpleNamespace(
            stdout="",
            stderr="Duration: 01:02:03.50, start: 0.000000, bitrate: 192 kb/s",
        )

    monkeypatch.setattr(local_media.subprocess, "run", fake_run)

    assert local_media.probe_local_duration("/tmp/song.mp3") == 3723.5


def test_imported_local_recent_stays_playable_after_original_delete(monkeypatch, tmp_path):
    source = tmp_path / "picked track.mp3"
    source.write_bytes(b"source-bytes")
    install_managed_media_root(monkeypatch, tmp_path)

    monkeypatch.setattr(local_media, "probe_local_title", lambda path: "Picked Track")
    monkeypatch.setattr(local_media, "probe_local_duration", lambda path: 123.5)
    install_transcode_success(monkeypatch, [b"normalized-bytes"])

    item = local_media.import_local_file(str(source))
    store = MemoryRecentStore()
    recent = RecentController(store=store, clock=lambda: 9.0)

    assert item is not None
    assert recent.record_item_played(item) is True

    source.unlink()
    recent.load()
    replay = recent.item_for_recent(item.cache_key)

    assert replay is not None
    assert replay.kind == "local"
    assert replay.tracks[0].local_path == item.tracks[0].local_path
