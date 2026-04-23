# Repository Guidelines

## Agent Rules
- Ask before risky, irreversible, or user-intent-sensitive choices. Otherwise, make a reversible assumption, state it, and continue.
- Preserve unrelated work; do not revert, commit, or push unless asked.

## Project Structure & Entry Points
- `yt_bar.py` is the LaunchAgent-compatible entry shim; keep it present because `install.sh` points at it.
- `yt_bar/app.py` contains `YTBar(rumps.App)` and `main()`.
- `yt_bar/audio_engine.py` contains the playback engine facade, worker loop, route/seek orchestration, and visualizer coordination.
- `yt_bar/av_session.py` owns the AVFoundation graph/session lifecycle used by `AudioEngine`.
- `yt_bar/decoder.py` contains the `yt-dlp` / `ffmpeg` decoder subprocess pipeline.
- `yt_bar/playback.py` owns current track, playlist advancement, playback mode/generation, and cache-trigger coordination state.
- `yt_bar/recent.py` owns recent-index state, menu-ready recent entries, stale pruning, and recent-to-playable-item conversion.
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
- Use `uv run` for project commands unless the environment has already been synced and activated.
- Use the project-local `.venv`; do not rely on global Python packages.
- Native playback bindings:
  - `pyobjc-framework-AVFoundation`
- External binaries required on `PATH`:
  - `yt-dlp`
  - `ffmpeg`
- macOS GUI requirements:
  - The app depends on `rumps` and AppKit.
  - Run it from the logged-in user session, not from a headless environment.
- Never commit secrets. Document required environment variables in `.env.example`, and keep real `.env` files out of git.
- Ask before changing the Python version, package manager, or tool configuration. Add task-required dependencies when needed, and report them.

## Development Commands
- Create/update environment: `uv sync`
- Launch app with local venv: `.venv/bin/python yt_bar.py`
- Alternate launch: `uv run python yt_bar.py`
- Test: `uv run pytest -q`
- Lint: `uv run ruff check .`
- Format check: `uv run ruff format --check .`
- Fix lint issues when requested: `uv run ruff check --fix .`
- Format code when requested: `uv run ruff format .`
- Type-check only if a type checker is configured; none is configured currently.
- Restart the installed menu bar app: `launchctl kickstart -k "gui/$(id -u)/com.wrinkledeth.yt-bar"`
- Syntax check: `.venv/bin/python -m compileall yt_bar.py yt_bar`
- Prefer file-scoped checks while iterating, for example `uv run pytest tests/test_smoke.py -q`, `uv run ruff check yt_bar/app.py`, and `uv run ruff format --check yt_bar/app.py`.

## Sandbox / Tooling Notes
- In restricted sandboxes, `uv` may fail if its default cache directory is not writable.
- If that happens, prefer `.venv/bin/python ...` for local checks, or set a writable cache dir such as `UV_CACHE_DIR=/tmp/uv-cache`.
- For restarts from Codex, prefer the installed LaunchAgent instead of `nohup` or other detached shell launches. The reliable path on this machine is `launchctl kickstart -k "gui/$(id -u)/com.wrinkledeth.yt-bar"`, then verify with `launchctl print "gui/$(id -u)/com.wrinkledeth.yt-bar"` or `ps -axo pid=,command= | rg "[y]t_bar\\.py"`.

## Architecture Notes
- `AudioEngine` (`yt_bar/audio_engine.py`) owns:
  - the public playback facade for `yt-dlp/ffmpeg or local-file/ffmpeg -> AVAudioEngine`
  - playback state
  - a single serial playback worker
  - elapsed time publication from `AVAudioPlayerNode` timing
  - CoreAudio default-output handling and route rebuild orchestration
  - local seek orchestration
  - stereometer dot-grid computation from a mixer tap snapshot
- `AVAudioGraphController` (`yt_bar/av_session.py`) owns:
  - `AVAudioEngine` / `AVAudioPlayerNode` / mixer / format lifecycle
  - `AVAudioEngineConfigurationChangeNotification` observer registration/removal
  - mixer tap installation/removal for visualizer snapshots
  - PCM buffer construction and `AVAudioPlayerNode` buffer scheduling
  - player play / pause / stop / prepare calls and rendered-frame timing reads
- `DecoderPipeline` (`yt_bar/decoder.py`) owns:
  - decoder thread startup/shutdown
  - `yt-dlp` / `ffmpeg` subprocess construction and cleanup
  - decoded PCM chunk queueing for `AudioEngine`
- `YTBar` (`yt_bar/app.py`) owns:
  - the `rumps` menu bar app
  - clipboard URL intake
  - resolver thread startup and resolver-to-playback handoff
  - playback/menu action routing
  - seek controls
  - progress updates
  - MediaPlayer remote-command / Now Playing integration
  - menu bar icon/title state
- `PlaybackController` (`yt_bar/playback.py`) owns:
  - hidden ordered playlist state
  - current track selection and playlist advancement
  - playback mode and current-item generation state
  - cache scheduling intent for newly started items
- `RecentController` (`yt_bar/recent.py`) owns:
  - recent-index persistence in `songs/recent.json`
  - cached-track pruning and recent item removal
  - playlist-recent replay conversion to cached playable items
- `CacheManager` (`yt_bar/cache.py`) owns:
  - delayed background caching into `songs/`
  - cache worker scheduling and duplicate suppression

## Threading Rules
- Treat AppKit and `rumps` UI state as main-thread-only.
- Background work currently includes:
  - URL resolution (`yt_bar/resolver.py`)
  - decoder subprocess I/O (`yt_bar/decoder.py`)
  - the serial playback worker
  - cache worker downloads (`yt_bar/cache.py`)
- Use the existing `_pending_actions` queue handoff pattern for UI mutations triggered by worker threads or MediaPlayer remote-command callbacks.
- `YTBar` and `PlaybackController` share the app `_state_lock`; preserve that lock boundary when changing current-track, playlist, or pending-action flow.
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
- Local-seek trace logging is disabled by default. Set `YT_BAR_SEEK_TRACE` to `1`, `true`, `yes`, or `on` at process start to enable `SEEK_TRACE_LOGGING` milestones on stdout.
- Playlist playback is intentionally hidden from the menu; preserve auto-advance even though there is no queue UI.
- Playback is decoded at a fixed internal `48 kHz stereo float32` format and the engine mixer converts to the active hardware format.
- Output-device handoff is driven by native route/config notifications, not polling.
- Cached tracks are downloaded as `.opus` into `songs/`, first to a `.partial` filename and then atomically renamed on success.
- Auto-start, if desired, is handled by repo-level `install.sh` / `uninstall.sh` scripts that manage the user LaunchAgent; it is not exposed as an in-app menu toggle.

## Current Requested Direction
- Treat `todo.md` as the current product-direction signal unless the user says otherwise.

## Editing Guidance
- Read existing docs and config before changing behavior.
- Prefer existing project commands over introducing new tools.
- Keep changes scoped to the requested task.
- Keep changes concentrated in the package module that owns the affected subsystem; preserve the root `yt_bar.py` shim.
- Preserve the main-thread UI safety model when changing playback or menu flow.
- Prefer small, explicit helpers over adding more stateful branching inline.
- If you change visible menu behavior, update `AGENTS.md` and `README.md` if the guidance here or user-facing docs become stale.
- If a command fails, report the command and relevant failure instead of guessing at the result.
- Report exactly which checks you ran and which relevant checks you skipped.

## Validation Expectations
- The automated suite currently starts with an import smoke test in `tests/test_smoke.py`.
- For Python changes, run the relevant focused pytest/Ruff checks first.
- Before handoff after broad code changes, run:
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `.venv/bin/python -m compileall yt_bar.py yt_bar`
- For app behavior changes, also verify the app launches on macOS and manually test the affected menu actions and title/icon states.
