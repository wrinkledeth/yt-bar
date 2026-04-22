# Repo Cleanup And Refactor Plan

## Current Focus

Phase 1 is complete. Phase 2 is the next active phase; later phases remain backlog context.

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
- Add a small focused test for the `YT_BAR_SEEK_TRACE` truthy-value parsing if Phase 2 test coverage includes configuration constants.

## Phase 1 Original Criteria

- Remove confirmed-unused code:
  - `YTDLP_FIELD_SEP`
  - `get_default_output_device_id`
  - `PlaybackSession.completion_count`
- Remove unused `py2app` from `pyproject.toml` project dependencies and refresh `uv.lock`.
- Gate seek trace logs behind `YT_BAR_SEEK_TRACE`; default off.
- Preserve current seek trace behavior only when the env value is one of `1`, `true`, `yes`, or `on`.

## Phase 2: Tests Before Refactors

- Add focused pure/module tests for resolver behavior, storage load/save and stale pruning, cache scheduling/download paths, and utils/visualizer formatting.
- Keep audio-engine behavioral tests minimal until after extraction; test pure predicates/helpers only if they become easy during cleanup.

## Phase 3: Typed Interfaces And Session State

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
