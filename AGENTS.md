# Repository Guidelines

## Project Structure & Entry Points
- `yt_bar.py` is the real application entrypoint and contains nearly all runtime logic.
- `main.py` is currently a placeholder and is not the menu bar app.
- `handoff.md` captures recent context and design notes; keep it aligned with major behavior changes when useful.
- `todo.md` tracks the current short-list of requested changes.

## What This Repo Does
- `yt-bar` is a macOS menu bar app that streams audio from YouTube URLs copied to the clipboard.
- Playback flows through `yt-dlp` to `ffmpeg`, then into a native macOS `AVAudioEngine` / `AVAudioPlayerNode` backend.
- Played items can be cached under `songs/` for later offline replay.
- The menu bar title shows a braille stereometer while audio is playing.

## Runtime Dependencies
- Python: `3.12+`
- Python packages: managed by `uv` and listed in `pyproject.toml`
- Native playback bindings:
  - `pyobjc-framework-AVFoundation`
- External binaries required on `PATH`:
  - `yt-dlp`
  - `ffmpeg`
- macOS GUI requirements:
  - The app depends on `rumps` and AppKit.
  - Run it from the logged-in user session, not from a headless environment.

## Development Commands
- Create/update environment: `uv sync`
- Launch app with local venv: `.venv/bin/python yt_bar.py`
- Alternate launch: `uv run python yt_bar.py`
- Syntax check: `.venv/bin/python -m py_compile yt_bar.py main.py`

## Sandbox / Tooling Notes
- In restricted sandboxes, `uv` may fail if its default cache directory is not writable.
- If that happens, prefer `.venv/bin/python ...` for local checks, or set a writable cache dir such as `UV_CACHE_DIR=/tmp/uv-cache`.

## Architecture Notes
- `AudioEngine` owns:
  - the `yt-dlp/ffmpeg or local-file/ffmpeg -> AVAudioEngine` pipeline
  - playback state
  - a single serial playback worker
  - elapsed time tracking from `AVAudioPlayerNode` timing
  - CoreAudio default-output and `AVAudioEngineConfigurationChangeNotification` handling
  - stereometer dot-grid computation from a mixer tap snapshot
- `YTBar` owns:
  - the `rumps` menu bar app
  - clipboard URL intake
  - hidden ordered playlist state
  - recent-index persistence in `songs/recent.json`
  - delayed background caching into `songs/`
  - seek controls
  - progress updates
  - MediaPlayer remote-command / Now Playing integration
  - menu bar icon/title state

## Threading Rules
- Treat AppKit and `rumps` UI state as main-thread-only.
- Background work currently includes:
  - URL resolution
  - decoder subprocess I/O
  - the serial playback worker
- Use the existing `_pending_actions` queue handoff pattern for UI mutations triggered by worker threads or MediaPlayer remote-command callbacks.
- Do not update menu items, titles, or other AppKit-driven state directly from playback or resolver threads.
- Do not mutate playback state directly from PyObjC callbacks, CoreAudio listeners, or mixer taps; those paths enqueue work back onto `AudioEngine`'s worker.

## Current User-Facing Behavior
- `Play from Clipboard` reads the clipboard and starts playback for an `http` URL.
- Single-video URLs resolve to one track.
- Playlist URLs resolve into a hidden ordered track list and auto-advance through tracks.
- Uncached items start streaming immediately and are cached in the background after a short listen threshold.
- Fully cached items can replay from local `.opus` files without network access.
- `Recent` shows up to 10 cached logical items.
- Playlist recents replay the cached subset of playlist tracks in order.
- If the macOS default audio output device changes during playback, the app rebuilds the native engine and resumes the current track from the captured position.
- Native macOS media commands integrate with the same playback helpers:
  - play / pause / toggle play-pause
  - skip forward `+30s`
  - skip backward `-30s`
- `nextTrackCommand` / `previousTrackCommand` also map to the same `±30s` seek helpers as a fallback for media surfaces that still route those actions as track-skip commands.
- The current track is published to Control Center / Now Playing via `MPNowPlayingInfoCenter`.
- The menu includes:
  - now playing title
  - progress line rendered as a fixed-width Unicode text bar plus elapsed/duration
  - play/pause
  - percentage-based seek submenu
  - `Recent`
  - `Play from Clipboard`
- Menu bar title states:
  - idle: `⠆⣿⠰`
  - paused/active without animation: `⣿⣿`
  - playing: live braille stereometer

## Seek Caveats
- Cached/local seek now reuses the active `AVAudioEngine` graph and aggressively replaces the old local `ffmpeg` decoder, so local replay skips do not wait on the normal 2-second subprocess shutdown path.
- Streamed seek still restarts playback with `ffmpeg -ss` on piped input.
- The streamed path remains slower for larger offsets because `ffmpeg` must consume data to reach the target.
- Seek now preserves paused state across both the menu submenu and media-key skip commands.
- `SEEK_TRACE_LOGGING` in `yt_bar.py` currently emits local-seek timing milestones to stdout for debugging.
- Playlist playback is intentionally hidden from the menu; preserve auto-advance even though there is no queue UI.
- Playback is decoded at a fixed internal `48 kHz stereo float32` format and the engine mixer converts to the active hardware format.
- Output-device handoff is driven by native route/config notifications, not polling.
- Cached tracks are downloaded as `.opus` into `songs/`, first to a `.partial` filename and then atomically renamed on success.
- Auto-start, if desired, is documented manually in `README.md` via a LaunchAgent plist; it is not exposed as an in-app menu toggle.

## Current Requested Direction
- The active TODOs are:
  - add a toggle for animation
  - change the starting icon to `⣿⣿`
- Treat `todo.md` as the current product-direction signal unless the user says otherwise.

## Editing Guidance
- Keep changes concentrated in `yt_bar.py` unless there is a clear reason to split code out.
- Preserve the main-thread UI safety model when changing playback or menu flow.
- Prefer small, explicit helpers over adding more stateful branching inline.
- If you change visible menu behavior, update both `handoff.md` and this file if the guidance here becomes stale.

## Validation Expectations
- There is no automated test suite yet.
- For code changes, at minimum:
  - run a syntax check
  - verify the app launches on macOS
  - manually test the affected menu actions and title/icon states
