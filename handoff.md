# yt-bar Handoff

## What it is
macOS menu bar music streamer. Copy a YouTube URL, click "Play from Clipboard", audio streams via yt-dlp -> ffmpeg -> a native `AVAudioEngine` / `AVAudioPlayerNode` backend. Played items cache into `songs/` for offline replay and a `Recent` submenu surfaces the last 10 cached logical items. Playlist URLs still play in order, but the playlist itself is hidden from the main playback UI. Playback follows macOS default output changes by rebuilding the engine and resuming from the captured position. Auto-start is documented manually in the README via a LaunchAgent plist rather than exposed in the app UI.

## Architecture
- **Single file**: `yt_bar.py`
- **rumps** for menu bar UI (must run on main thread)
- **yt-dlp** subprocess streams audio to **ffmpeg** which decodes to raw PCM
- **AVAudioEngine** plays fixed-format `48 kHz stereo float32` PCM audio
- **numpy** for Mid/Side stereometer computation
- Braille Unicode chars (U+2800-U+28FF) render a 6x4 dot grid as 3 characters

## Key Classes
- `AudioEngine`: manages yt-dlp -> ffmpeg -> AVFoundation pipeline, owns the serial playback worker, CoreAudio route handling, native timing, and mixer-tap visualizer snapshots
- `YTBar(rumps.App)`: menu bar UI, hidden playlist state, delayed background cache workers, recent-index persistence, timers for viz (70ms) and progress (1s), plus MediaPlayer remote-command / Now Playing integration

## Threading Model
- **Main thread**: rumps event loop, all UI modifications
- **Background threads**: URL resolution (`_resolve_and_play`), decoder subprocess I/O, serial playback worker
- **`_pending_actions` queue**: background threads and remote-command handlers enqueue UI actions, then the viz timer (main thread) drains and executes them. This keeps AppKit mutations off background threads while allowing more than one pending action at a time.
- PyObjC callbacks, CoreAudio listeners, and mixer taps do not mutate playback state inline; they enqueue work back onto the playback worker.

## Menu Layout
```
Now Playing: Song Title
━━━━━━●───────────────  0:45 / 3:00
---
Play / Pause
Seek (submenu with 0%-90% clickable segments)
Recent (submenu with cached items)
---
Play from Clipboard
```

## Icons
- Idle: `⠆⣿⠰`
- Playing: live braille stereometer (Mid/Side dot cloud)
- Paused: `⣿⣿`

## Seek Implementation
- `_seek_to_pct(pct)`: uses a fast local-seek path for cached tracks and falls back to full playback restart for streamed tracks
- Menu seek and media-key seek now preserve paused state: a paused track stays paused at the new position.
- Progress now tracks the seek target immediately and then continues from the correct offset once playback resumes or while paused at the new position.
- Cached `.opus` replay now keeps the existing `AVAudioEngine` alive and aggressively kills/restarts the local decoder process on seek.
- The real local-latency culprit was the old decoder shutdown path waiting on the normal subprocess timeout, not local decode startup itself.
- `-ss` with piped input is still used for streamed playback and remains slower because ffmpeg reads and discards data to reach offset.
- `SEEK_TRACE_LOGGING` currently logs local seek milestones (`requested`, `decoder_restarted`, `first_pcm_chunk`, `first_buffer_scheduled`, `player_play_called`, `first_elapsed_advance`) to stdout while this path is being validated.

## Native Media Integration
- `MPRemoteCommandCenter` handlers are registered through PyObjC against the system `MediaPlayer.framework`
- `play`, `pause`, and `togglePlayPause` map to the same shared helpers as the menu item
- `skipForward` / `skipBackward` use a `30s` preferred interval and reuse the same seek path as the menu
- `nextTrackCommand` / `previousTrackCommand` also map to the same `30s` seek helpers as a fallback for hardware keys and other surfaces that keep routing skip presses as track-skip commands
- `MPNowPlayingInfoCenter` is populated with title, duration, elapsed time, and playback rate so the current track appears in Control Center / Now Playing
- If MediaPlayer setup fails, the app logs the failure and continues without media-key support

## Known Issues / Future Work
- Seek via `-ss` on piped ffmpeg is slow for large offsets
- Route changes still require a brief rebuild/restart; this is correctness-first, not gapless handoff
- First-play caching still uses a second yt-dlp fetch after the listen threshold; there is no tee/single-fetch cache path yet
- Media-key routing is subject to normal macOS Now Playing arbitration when other media apps are active
- No volume control (intentional per user preference)
- Playlist playback is hidden; there is no visible queue/count indicator
- App must be launched from user's terminal (not Claude's) to get proper macOS GUI context

## Dependencies (pyproject.toml)
- rumps, numpy, py2app, pyobjc-framework-AVFoundation
- MediaPlayer integration is loaded dynamically from the system `MediaPlayer.framework`; no separate PyPI package is required for the current implementation
- External: yt-dlp, ffmpeg (must be on PATH)

## Running
```
uv run python yt_bar.py
```

## Recent Changes
1. Fixed background-thread UI crash: all menu modifications now dispatched to main thread via a pending-action handoff
2. Idle icon changed to `⠆⣿⠰`
3. Removed visible queue UI and `-30s` / `+30s` controls while keeping playlists auto-advancing internally
4. Fixed per-track progress to stay aligned after seek and refreshed the progress bar styling
5. Replaced `sounddevice` / PortAudio with a native `AVAudioEngine` backend, plus CoreAudio default-output listening and engine-configuration rebuilds
6. Stereometer now reads from an `AVAudioEngine` mixer tap instead of the old output stream write loop
7. Added `songs/`-backed offline caching plus a `Recent` submenu for cached video and playlist replay
8. Removed the in-app launch-at-login toggle and documented manual LaunchAgent setup in the README instead
9. Replaced the old bracketed progress bar with a fixed-width Unicode text row: `━━━━●────  elapsed / duration`
10. Added native macOS media-command support and Control Center Now Playing integration while keeping the existing `Seek` submenu
11. Fixed cached local seek latency by bypassing the old 2-second decoder shutdown wait before starting the replacement local decoder
