# yt-bar

`yt-bar` is a macOS menu bar app that streams audio from YouTube URLs in your clipboard.

It uses:
- `yt-dlp` to resolve and stream audio
- `ffmpeg` to decode PCM
- `rumps` for the menu bar UI
- a native macOS `AVAudioEngine` / `AVAudioPlayerNode` backend for playback

## Current Behavior

- `Play from Clipboard` reads an `http` URL from the clipboard and starts playback.
- Single-video URLs resolve to one track.
- Playlist URLs resolve into a hidden ordered track list and auto-advance in order.
- The menu bar title shows a braille stereometer while audio is playing.
- If the macOS default output device changes during playback, the app rebuilds the native engine and resumes from the current position.
- Seek works by restarting playback with `ffmpeg -ss`, so large jumps can still be slow.

## Requirements

- macOS
- Python `3.12+`
- `yt-dlp` on `PATH`
- `ffmpeg` on `PATH`

Python dependencies are managed through `uv` and listed in [pyproject.toml](/Users/zen/dev/yt-bar/pyproject.toml:1).

## Setup

```bash
uv sync
```

## Run

```bash
.venv/bin/python yt_bar.py
```

or

```bash
uv run python yt_bar.py
```

This must run in your logged-in macOS GUI session, not in a headless environment.

## Development Notes

- Real app entrypoint: [yt_bar.py](/Users/zen/dev/yt-bar/yt_bar.py:1)
- `main.py` is currently a placeholder.
- Playback is decoded at a fixed internal `48 kHz stereo float32` format and the engine mixer converts to the active hardware format.
- AppKit / `rumps` UI changes should stay on the main thread.

## Validation

```bash
.venv/bin/python -m py_compile yt_bar.py main.py
```
