# Repo Cleanup And Refactor Plan

## Current Focus

Phase 1, Phase 2, and Phase 3 are complete. Phase 4 is the next active phase;
later phases remain backlog context.

## Phase 1: Cleanup - Done

- Commit: `5f39e3d`
- Completed:
  - Removed confirmed-unused code:
    - `YTDLP_FIELD_SEP`
    - `get_default_output_device_id`
    - `PlaybackSession.completion_count`
  - Removed unused `py2app` from `pyproject.toml` project dependencies and refreshed `uv.lock`.
  - Gated seek trace logs behind `YT_BAR_SEEK_TRACE`; default off.
  - Preserved seek trace behavior only when the env value is one of `1`, `true`, `yes`, or `on`.
- Validation passed:
  - `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .`
  - `.venv/bin/python -m compileall yt_bar.py yt_bar`
  - Manual env sanity: default seek tracing is `False`; `YT_BAR_SEEK_TRACE=yes` makes it `True`.

## Phase 1 Decisions And Deviations

- `SEEK_TRACE_LOGGING` remains an import-time constant. The `YT_BAR_SEEK_TRACE` value is read at process start/module import, not dynamically during playback.
- `uv.lock` was refreshed offline with `UV_CACHE_DIR=/tmp/uv-cache`; no dependency download was needed.
- `AGENTS.md` was intentionally left unchanged during Phase 1 per the prior assumption to defer broader docs updates until the module layout is settled.

## Phase 1 Discoveries For Future Phases

- `core_audio.py` is now listener-only; future CoreAudio refactors should not assume an available default-output device query helper.
- With `py2app` removed, future packaging or LaunchAgent work should rely on the existing script/venv path unless a packaging tool is explicitly reintroduced.
- Tests that need to assert seek tracing should isolate import-time environment state, for example by subprocess or module reload.

## Out-Of-Scope Follow-Ups

- Update the `AGENTS.md` seek caveat because the current line saying `SEEK_TRACE_LOGGING` emits logs by default is now stale.
- Consider documenting `YT_BAR_SEEK_TRACE` in README or `.env.example` if user-facing troubleshooting docs are expanded.
- Add a small focused test for the `YT_BAR_SEEK_TRACE` truthy-value parsing if configuration constants get their own test coverage.

## Phase 1 Original Criteria

- Remove confirmed-unused code:
  - `YTDLP_FIELD_SEP`
  - `get_default_output_device_id`
  - `PlaybackSession.completion_count`
- Remove unused `py2app` from `pyproject.toml` project dependencies and refresh `uv.lock`.
- Gate seek trace logs behind `YT_BAR_SEEK_TRACE`; default off.
- Preserve current seek trace behavior only when the env value is one of `1`, `true`, `yes`, or `on`.

## Phase 2: Tests Before Refactors - Done

- Commit: `9f6b7f2`
- Completed:
  - Added focused resolver tests for URL source selection, YouTube fallback URLs, track construction, playlist resolution, and playlist-to-single fallback.
  - Added focused storage tests for settings defaults/round trips, recent-index load/save ordering, and stale cached-track pruning.
  - Added focused cache tests for delayed-cache gating, job scheduling, duplicate suppression, successful partial-file promotion, and failed-download cleanup.
  - Added focused utils/visualizer tests for cache keys, duration/time formatting, title truncation, progress bars, and braille grid rendering.
- Validation passed:
  - `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .`
  - `.venv/bin/python -m compileall yt_bar.py yt_bar`
- Manual app launch/UI playback checks were skipped because Phase 2 only added pure/module tests and did not change production app behavior.

## Phase 2 Decisions And Deviations

- No production code was changed during Phase 2.
- Resolver tests isolate `yt-dlp` behavior by monkeypatching `run_yt_dlp_json`; they do not require network access or external binaries.
- Cache tests intentionally cover current underscored `CacheManager` scheduling/download helpers to pin behavior before structural refactors; this is test coverage for current module internals, not a decision to keep those helpers private forever.
- Cache download tests monkeypatch `yt_bar.models.partial_cache_abspath_for_id` so partial-file assertions stay inside `tmp_path`.
- Audio-engine behavioral tests remained out of scope, matching the original Phase 2 guidance to wait until after extraction.

## Phase 2 Discoveries For Future Phases

- `TrackInfo.partial_local_path` is derived from the global `SONGS_DIR` path via `partial_cache_abspath_for_id`, while `CacheManager` accepts an injected `songs_dir`; future cache extraction should align cache path ownership.
- `CacheManager.enqueue_cache_jobs_for_item` refreshes recent metadata before checking whether any track actually needs a cache job; future refactors should preserve or intentionally change that side effect.
- `resolve_playlist` uses the original entry index for fallback titles, so skipped non-dict entries can make titles jump, for example from `Track 1` to `Track 3`.
- Importing `yt_bar.utils` still imports AppKit/Foundation because `_set_header_title` lives beside pure formatting helpers; future pure-test portability would improve if UI header rendering moved behind a smaller bridge module.
- `grid_to_braille` treats the threshold as strictly greater than `0.18`; values equal to `0.18` remain unlit.

## Phase 2 Out-Of-Scope Follow-Ups

- Add configuration-constant tests for `YT_BAR_SEEK_TRACE` truthy-value parsing.
- Consider splitting `_set_header_title` out of `yt_bar.utils` if future refactors aim to keep pure formatting helpers independent of PyObjC imports.
- Consider making partial-cache path construction a single responsibility owned by the cache subsystem, especially before splitting cache/download modules.

## Phase 2 Original Criteria

- Add focused pure/module tests for resolver behavior, storage load/save and stale pruning, cache scheduling/download paths, and utils/visualizer formatting.
- Keep audio-engine behavioral tests minimal until after extraction; test pure predicates/helpers only if they become easy during cleanup.

## Phase 3: Typed Interfaces And Session State - Done

- Commit: `ca13d3e`
- Completed:
  - Added a typed `UICommand` / `UICommandKind` model for pending UI actions.
  - Updated remote command dispatch, UI action enqueueing, and UI action handling to use typed commands.
  - Split `PlaybackSession` runtime state into graph, decoder, schedule, route, and seek-trace dataclasses.
  - Preserved the existing public `AudioEngine` methods and `PlaybackSession` request proxy properties.
  - Added focused model tests for typed command factories and per-session runtime state isolation.
- Validation passed:
  - `UV_CACHE_DIR=/tmp/uv-cache uv run pytest -q`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff check .`
  - `UV_CACHE_DIR=/tmp/uv-cache uv run ruff format --check .`
  - `.venv/bin/python -m compileall yt_bar.py yt_bar`
  - Restarted the installed LaunchAgent and verified `launchctl print` reported `state = running`.
- Manual playback/menu/media-key checks were skipped beyond launch verification.

## Phase 3 Decisions And Deviations

- The typed pending-action model lives in `yt_bar.models` with the other shared dataclasses.
- Remote command callbacks enqueue generic UI commands such as play, pause, toggle, and seek-delta; there are no longer separate `remote_*` action strings.
- The existing pending-action queue and `_state_lock` handoff remain the UI-thread boundary.
- `PlaybackSession` still exposes request-backed proxy properties such as `url`, `duration`, `paused`, and `base_offset_seconds` so the `AudioEngine` refactor stayed mechanical.
- Audio-engine behavioral tests remained out of scope; Phase 3 added model-focused coverage only.

## Phase 3 Discoveries For Future Phases

- The new `PlaybackSession` groups map cleanly to likely Phase 4 extraction boundaries:
  graph state for AVFoundation session ownership, decoder state for subprocess/queue ownership, and schedule/route/seek-trace state for playback coordination helpers.
- Future dataclasses should avoid field names that shadow imported modules during annotation evaluation; `PlaybackDecoderState.queue` required an import alias for the `queue` module.
- Launch log files do not include timestamps on each line, so launch verification should check file modification times before treating old stderr/stdout lines as current failures.
- `UICommand` can be reused or moved if Phase 4 introduces explicit menu action snapshots.

## Phase 3 Out-Of-Scope Follow-Ups

- Manually test clipboard playback, pause/resume, seek, recents, compact menu, media keys, and title states after the next UI-touching change.
- Add audio-engine behavioral tests for local seek, route rebuild, and decoder failure paths once the decoder/AVFoundation extraction makes those seams easier to isolate.
- Consider moving `UICommand` into a dedicated action or controller module if Phase 4 expands the command surface beyond the current pending UI queue.

## Phase 3 Original Criteria

- Replace pending UI action strings with a typed command model.
- Update remote command dispatch, UI action enqueueing, and UI action handling to use that model.
- Split `PlaybackSession` state mechanically into smaller dataclasses while preserving public `AudioEngine` behavior.

## Phase 4: Structural Refactors

- Decouple `MenuController` first with explicit menu actions and menu state snapshots.
- Extract `YTBar` responsibilities into recent and playback coordination modules.
- Split `AudioEngine` last into decoder, AVFoundation session, and stereometer-focused modules while keeping `AudioEngine` as the public facade.

## Validation

- After each phase: `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format --check .`, `.venv/bin/python -m compileall yt_bar.py yt_bar`.
- After UI/playback phases: launch the app and manually verify clipboard playback, pause/resume, seek, recents, compact menu, media keys, and title states.

## Assumptions

- `py2app` has no current intended use and should be removed.
- No user-facing behavior changes are intended except seek tracing becoming opt-in.
- Update `AGENTS.md` only after the final module layout is settled.
