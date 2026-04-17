# yt-bar

`yt-bar` is a macOS menu bar app that streams audio from YouTube URLs in your clipboard and caches them for offline replay.

It uses:
- `yt-dlp` to resolve and stream audio
- `ffmpeg` to decode PCM
- `rumps` for the menu bar UI
- a native macOS `AVAudioEngine` / `AVAudioPlayerNode` backend for playback

## Current Behavior

- `Play from Clipboard` reads an `http` URL from the clipboard and starts playback.
- Single-video URLs resolve to one track.
- Playlist URLs resolve into a hidden ordered track list and auto-advance in order.
- Uncached items start streaming immediately, then cache into `songs/` after a short listen threshold.
- Fully cached items play from local `.opus` files instead of the network.
- `Recent` shows the 10 most recently played cached items and replays them offline.
- Playlist recents appear as one item and replay the cached subset in playlist order.
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
- Cached audio and the recent index live under `songs/`.
- Playback is decoded at a fixed internal `48 kHz stereo float32` format and the engine mixer converts to the active hardware format.
- AppKit / `rumps` UI changes should stay on the main thread.

## Launch At Login

There is no in-app auto-start toggle. If you want `yt-bar` to launch at login, create a user LaunchAgent manually:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>com.wrinkledeth.yt-bar</string>
  <key>ProgramArguments</key>
  <array>
    <string>/Users/zen/dev/yt-bar/.venv/bin/python</string>
    <string>/Users/zen/dev/yt-bar/yt_bar.py</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>ProcessType</key>
  <string>Interactive</string>
  <key>WorkingDirectory</key>
  <string>/Users/zen/dev/yt-bar</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>/Users/zen/dev/yt-bar/yt-bar.launchd.log</string>
  <key>StandardErrorPath</key>
  <string>/Users/zen/dev/yt-bar/yt-bar.launchd.err.log</string>
</dict>
</plist>
```

Save it as `~/Library/LaunchAgents/com.wrinkledeth.yt-bar.plist`, then load it:

```bash
launchctl bootstrap "gui/$(id -u)" ~/Library/LaunchAgents/com.wrinkledeth.yt-bar.plist
launchctl enable "gui/$(id -u)/com.wrinkledeth.yt-bar"
```

To disable it later:

```bash
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/com.wrinkledeth.yt-bar.plist
rm ~/Library/LaunchAgents/com.wrinkledeth.yt-bar.plist
```

If the repo path or virtualenv path changes, update the plist to match.

## Validation

```bash
.venv/bin/python -m py_compile yt_bar.py main.py
```
