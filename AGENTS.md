# Repository Guidelines

## Project Structure & Entry Points
- `yt_bar.py` is the LaunchAgent-compatible entry shim; keep it present because `install.sh` points at it.
- `yt_bar/app.py` contains `YTBar(rumps.App)` and `main()`.
- `yt_bar/audio_engine.py` contains the playback engine and decoder pipeline.
- `yt_bar/menu.py`, `yt_bar/cache.py`, `yt_bar/remote_commands.py`, `yt_bar/storage.py`, and `yt_bar/resolver.py` own the corresponding app subsystems.
- `yt_bar/constants.py`, `yt_bar/models.py`, `yt_bar/utils.py`, `yt_bar/objc_bridges.py`, `yt_bar/core_audio.py`, `yt_bar/media_player.py`, and `yt_bar/visualizer.py` provide shared helpers and platform bridges.
- `CLAUDE.md` should stay a relative symlink to `AGENTS.md`.
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
- Restart the installed menu bar app: `launchctl kickstart -k "gui/$(id -u)/com.wrinkledeth.yt-bar"`
- Syntax check: `.venv/bin/python -m compileall yt_bar.py yt_bar`

## Sandbox / Tooling Notes
- In restricted sandboxes, `uv` may fail if its default cache directory is not writable.
- If that happens, prefer `.venv/bin/python ...` for local checks, or set a writable cache dir such as `UV_CACHE_DIR=/tmp/uv-cache`.
- For restarts from Codex, prefer the installed LaunchAgent instead of `nohup` or other detached shell launches. The reliable path on this machine is `launchctl kickstart -k "gui/$(id -u)/com.wrinkledeth.yt-bar"`, then verify with `launchctl print "gui/$(id -u)/com.wrinkledeth.yt-bar"` or `ps -axo pid=,command= | rg "[y]t_bar\\.py"`.

## Architecture Notes
- `AudioEngine` (`yt_bar/audio_engine.py`) owns:
  - the `yt-dlp/ffmpeg or local-file/ffmpeg -> AVAudioEngine` pipeline
  - playback state
  - a single serial playback worker
  - elapsed time tracking from `AVAudioPlayerNode` timing
  - CoreAudio default-output and `AVAudioEngineConfigurationChangeNotification` handling
  - stereometer dot-grid computation from a mixer tap snapshot
- `YTBar` (`yt_bar/app.py`) owns:
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
  - URL resolution (`yt_bar/resolver.py`)
  - decoder subprocess I/O (`yt_bar/audio_engine.py`)
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
  - now playing title with a `◌` (streaming) or `●` (cached) playback-mode badge
  - progress line rendered as a fixed-width Unicode text bar plus elapsed/duration
  - `Play from Clipboard`
  - `Recent`
  - dynamic transport item: `Pause`, `Resume`, or `Play`
  - percentage-based `Seek` submenu, enabled only while an active track has a known duration
  - `Settings` with `Compact Menu`, `Skip Interval`, and `Recent List Size`
- `Compact Menu` hides the top-level transport item and `Seek`, leaving `Play from Clipboard`, `Recent`, and `Settings` as the main actionable menu items.
- Header rows (`_now_playing`, `_progress`) use `NSAttributedString` with `secondaryLabelColor` so they render visually distinct from actionable items. They are enabled items with no-op callbacks so AppKit honors the attributed color.
- Menu bar title states:
  - idle: `⠆⣿⠰`
  - paused/active without animation: `⠀⠶⠀`
  - playing: live braille stereometer

## Seek Caveats
- Cached/local seek now reuses the active `AVAudioEngine` graph and aggressively replaces the old local `ffmpeg` decoder, so local replay skips do not wait on the normal 2-second subprocess shutdown path.
- Streamed seek still restarts playback with `ffmpeg -ss` on piped input.
- The streamed path remains slower for larger offsets because `ffmpeg` must consume data to reach the target.
- Seek now preserves paused state across both the menu submenu and media-key skip commands.
- `SEEK_TRACE_LOGGING` in `yt_bar/constants.py` currently emits local-seek timing milestones to stdout for debugging.
- Playlist playback is intentionally hidden from the menu; preserve auto-advance even though there is no queue UI.
- Playback is decoded at a fixed internal `48 kHz stereo float32` format and the engine mixer converts to the active hardware format.
- Output-device handoff is driven by native route/config notifications, not polling.
- Cached tracks are downloaded as `.opus` into `songs/`, first to a `.partial` filename and then atomically renamed on success.
- Auto-start, if desired, is handled by repo-level `install.sh` / `uninstall.sh` scripts that manage the user LaunchAgent; it is not exposed as an in-app menu toggle.

## Current Requested Direction
- Treat `todo.md` as the current product-direction signal unless the user says otherwise.

## Editing Guidance
- Keep changes concentrated in the package module that owns the affected subsystem; preserve the root `yt_bar.py` shim.
- Preserve the main-thread UI safety model when changing playback or menu flow.
- Prefer small, explicit helpers over adding more stateful branching inline.
- If you change visible menu behavior, update `AGENTS.md` and `README.md` if the guidance here or user-facing docs become stale.

## Validation Expectations
- There is no automated test suite yet.
- For code changes, at minimum:
  - run a syntax check
  - verify the app launches on macOS
  - manually test the affected menu actions and title/icon states
