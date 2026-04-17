# yt-bar

`yt-bar` exists because sometimes YouTube is the music app and switching back to a browser every time you want to play, pause, skip, or change outputs is annoying. It lives in the macOS menu bar, starts from the clipboard, works with the media keys, and saves tracks locally once you stream them. The whole point is: minimal clutter, fast access, no tab juggling.

## Install

Requirements:
- macOS
- Python `3.12+`
- `uv`
- `yt-dlp` on `PATH`
- `ffmpeg` on `PATH`

Install the Python env:

```bash
uv sync
```

Run it once directly:

```bash
.venv/bin/python yt_bar.py
```

Or install it as a LaunchAgent so it starts cleanly in your logged-in GUI session:

```bash
./install.sh
```

Remove the LaunchAgent later with:

```bash
./uninstall.sh
```

## Usage

- Copy a YouTube video or playlist URL and use `Play from Clipboard`.
- Playback starts streaming immediately and the track gets saved locally after you listen, so later replays can come from disk.
- F7 / F8 / F9 media keys work, so after playback starts the app is mostly hotkey-driven.
- Cached/local playback seeks much faster than streamed playback.
- If your macOS output device changes mid-playback, `yt-bar` follows it and resumes from the same spot. AirPods handoff is the intended case.
- `Recent` replays cached items. Hold `Option` in that menu to reveal remove actions for items you do not want there anymore.
- The badge next to the song title shows playback source: `◌` means streaming, `●` means local cache.
- `Settings` lets you toggle `Compact Menu`, change skip interval seconds, and change the max recent-list size.
- Playlists still auto-advance in order, but the queue stays hidden.
