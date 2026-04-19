# yt-bar Package Refactor Plan

## Summary

Refactor `yt_bar.py` into a `yt_bar/` package in behavior-preserving stages while keeping the root `yt_bar.py` LaunchAgent entrypoint. Prioritize mechanical moves first, isolate risky extractions, and defer behavior cleanups like rewriting `AudioEngine._handle_command`.

The root `yt_bar.py` shim and `yt_bar/` package will intentionally coexist; Python resolves `from yt_bar.app import main` to the package directory while the root script runs as `__main__`.

## Key Changes

- Keep `songs/`, `settings.json`, and `recent.json` under the repo root by computing `APP_ROOT` as the package parent.
- Keep `yt_bar/__init__.py` minimal until `yt_bar/app.py` exists.
- Put shared domain data in `models.py`; keep `MediaPlayerSupport` in `media_player.py`.
- Extract `CacheManager` only with a documented boundary:
  - `CacheManager` may use private locks for its own internals.
  - `CacheManager` never acquires `YTBar._state_lock`.
  - `YTBar` never calls into `CacheManager` while holding `_state_lock`.
  - `CacheManager` never invokes app callbacks while holding its own lock.
  - Cache completion must trigger Recent rebuilds through the existing main-thread handoff, not direct menu mutation.
- Keep AVAudioEngine mixer tap lifecycle in `AudioEngine`; move only clearly pure visualizer helpers early.
- Treat the `AudioEngine` module move as the highest-risk structural commit and pause for a longer smoke before stacking further refactors.

## Implementation Sequence

1. **Foundation commit**
   - Add package skeleton, constants, shared models, and pure utils.
   - Preserve existing JSON serialization exactly.
   - Move only pure visualizer helpers such as `grid_to_braille`; defer stateful stereometer extraction.
2. **Platform bridge commit**
   - Move ObjC bridge classes, common-mode timer helper, CoreAudio ctypes helpers, and MediaPlayer loader.
   - Keep `MediaPlayerSupport` local to `media_player.py`.
3. **AudioEngine commit**
   - Move `AudioEngine` to `yt_bar/audio_engine.py` without changing control flow.
   - Do not rewrite `_handle_command`.
   - Run an extended smoke before continuing.
4. **Resolver commit**
   - Move yt-dlp URL resolution into `resolver.py`.
   - Expose `resolve_url(url) -> ResolvedItem | None`.
5. **Storage commit**
   - Add `SettingsStore` and `RecentStore`.
   - Preserve existing on-disk schemas and locking behavior.
6. **Cache commit**
   - Add `cache.py` with the lock and callback contract documented at the top.
   - Move cache workers, delayed caching, partial-file cleanup, scheduled IDs, and shutdown.
   - Verify same-session Recent menu rebuild after cache completion.
7. **Remote command commit**
   - Add `RemoteCommandController`.
   - Preserve enqueue-only MediaPlayer callbacks and Now Playing behavior.
8. **Menu commit**
   - Add `MenuController` for layout, Recent menu, settings checkmarks, seek markers, progress display, and enabled states.
   - Keep all rumps/AppKit mutation on the main thread.
9. **Final app/shim/docs commit**
   - Move slim `YTBar`, signal handling, and `main()` into `yt_bar/app.py`.
   - Replace root `yt_bar.py` with the shim.
   - Add `yt_bar/__main__.py`.
   - Update `AGENTS.md` and `handoff.md`.
   - Delete dead `main.py`.

## Test Plan

Per phase, use checks that match modules that exist:

- Early: `.venv/bin/python -m compileall yt_bar.py yt_bar`
- Early imports: `import yt_bar.constants`, `yt_bar.models`, `yt_bar.utils`
- Bridge imports: `import yt_bar.objc_bridges`, `yt_bar.core_audio`, `yt_bar.media_player`
- Engine import: `import yt_bar.audio_engine`
- Final imports: `import yt_bar`, `import yt_bar.app`, and `.venv/bin/python -m yt_bar`

Required smoke gates:

- Existing `songs/recent.json` populates Recent identically after model/storage extraction.
- `AudioEngine` move: stream play, cached play, seek, pause/resume, output-device change, quit/relaunch.
- Media keys: F7/F8/F9 exercise play/pause and seek through MediaPlayer and `_pending_actions`.
- First-cache path: play uncached URL past cache delay, confirm cache file lands, open Recent in the same session and see the item, then replay it from Recent with cached `●` mode.
- Storage round-trip: change settings, create/update Recent, quit cleanly, relaunch, confirm settings and Recent survive unchanged.
- Final LaunchAgent check: `launchctl kickstart -k "gui/$(id -u)/com.wrinkledeth.yt-bar"` and confirm no import errors in logs.

## Assumptions

- This refactor is behavior-preserving; `todo.md` product changes are deferred.
- No automated test suite is added.
- `_handle_command` refactor is allowed only after one full day of normal use plus playlist cold start, cached replay, seek while paused, media-key input, first-cache handoff, LaunchAgent restart, storage round-trip, and mid-playback output-device change with no tracebacks.
