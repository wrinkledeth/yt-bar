# Repository Guidelines

## Agent Rules
- Ask before risky, irreversible, or user-intent-sensitive choices. Otherwise, make a reversible assumption, state it, and continue.
- Preserve unrelated work; do not revert, commit, or push unless asked.

### 1. Think Before Coding

**Don't assume. Don't hide confusion. Surface tradeoffs.**

Before implementing:
- State your assumptions explicitly. If uncertain, ask.
- If multiple interpretations exist, present them - don't pick silently.
- If a simpler approach exists, say so. Push back when warranted.
- If something is unclear, stop. Name what's confusing. Ask.

### 2. Simplicity First

**Minimum code that solves the problem. Nothing speculative.**

- No features beyond what was asked.
- No abstractions for single-use code.
- No "flexibility" or "configurability" that wasn't requested.
- No error handling for impossible scenarios.
- If you write 200 lines and it could be 50, rewrite it.

Ask yourself: "Would a senior engineer say this is overcomplicated?" If yes, simplify.

### 3. Surgical Changes

**Touch only what you must. Clean up only your own mess.**

When editing existing code:
- Don't "improve" adjacent code, comments, or formatting.
- Don't refactor things that aren't broken.
- Match existing style, even if you'd do it differently.
- If you notice unrelated dead code, mention it - don't delete it.

When your changes create orphans:
- Remove imports/variables/functions that YOUR changes made unused.
- Don't remove pre-existing dead code unless asked.

The test: Every changed line should trace directly to the user's request.

### 4. Goal-Driven Execution

**Define success criteria. Loop until verified.**

Transform tasks into verifiable goals:
- "Add validation" → "Write tests for invalid inputs, then make them pass"
- "Fix the bug" → "Write a test that reproduces it, then make it pass"
- "Refactor X" → "Ensure tests pass before and after"

For multi-step tasks, state a brief plan:
```
1. [Step] → verify: [check]
2. [Step] → verify: [check]
3. [Step] → verify: [check]
```

Strong success criteria let you loop independently. Weak criteria ("make it work") require constant clarification.


## Repo Essentials
- `yt_bar.py` must stay present because `install.sh` launches it directly.
- `CLAUDE.md` should remain a relative symlink to `AGENTS.md`.
- Treat `todo.md` as the current product-direction signal unless the user says otherwise.
- `yt-bar` is a macOS menu bar app that streams YouTube audio, imports local audio files into `songs/`, and renders a braille stereometer in the menu bar while playing.

## Environment
- Python `3.12+`; use the repo `.venv` and `uv`.
- Runtime dependencies on `PATH`: `yt-dlp`, `ffmpeg`.
- The app depends on `rumps` and AppKit and must run in the logged-in macOS GUI session, not headless.
- Ask before changing the Python version, package manager, or tool configuration.

## Commands
- Sync env: `uv sync`
- Launch locally: `.venv/bin/python yt_bar.py`
- Full checks:
  - `uv run pytest -q`
  - `uv run ruff check .`
  - `uv run ruff format --check .`
  - `.venv/bin/python -m compileall yt_bar.py yt_bar`
- Prefer focused pytest/Ruff checks while iterating.

## Sandbox / Tooling Notes
- In restricted sandboxes, `uv` may need `UV_CACHE_DIR=/tmp/uv-cache`; otherwise prefer `.venv/bin/python ...` for local checks.
- To restart the installed app, prefer the LaunchAgent:
  - `launchctl kickstart -k "gui/$(id -u)/com.wrinkledeth.yt-bar"`
  - verify with `launchctl print "gui/$(id -u)/com.wrinkledeth.yt-bar"` or `ps -axo pid=,command= | rg "[y]t_bar\\.py"`

## Key Invariants
- Treat AppKit and `rumps` UI state as main-thread-only.
- Use the existing `_pending_actions` queue for UI mutations triggered from worker threads or MediaPlayer callbacks.
- `YTBar` and `PlaybackController` share `_state_lock`; preserve that lock boundary when changing current-track, playlist, or pending-action flow.
- Do not mutate playback state directly from PyObjC callbacks, CoreAudio listeners, or mixer taps; enqueue work back onto `AudioEngine`'s worker.
- Keep playlist navigation limited to the separate `Songs` menu for the currently loaded playlist, and preserve ordered auto-advance.
- Preserve the remote-command fallback where `nextTrackCommand` / `previousTrackCommand` map to the same seek helpers as skip forward/back.
- Keep `songs/` as the single managed media root; single-item media stays directly under it, playlist imports use readable subfolders, new managed files use readable `.opus` names with stable hash suffixes, and existing files are left in place.
- Keep changes concentrated in the package module that owns the subsystem; preserve the root `yt_bar.py` shim.

## Validation
- Automated tests cover menu rendering/action dispatch, clipboard intake routing, remote commands, and Now Playing payloads.
- Keep real menu-bar interaction, media-key delivery, pasteboard behavior, native audio output/device handoff, external `yt-dlp` / `ffmpeg`, and live YouTube URLs as manual or integration validation.
- For Python changes, run focused checks first. Before handoff after broader code changes, run the full checks above.
- If you change visible app behavior, update `README.md` and `AGENTS.md` if the docs become stale.
- Report exactly which checks you ran and which you skipped.
