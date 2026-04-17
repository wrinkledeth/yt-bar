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
- `YTBar(rumps.App)`: menu bar UI, hidden playlist state, delayed background cache workers, recent-index persistence, timers for viz (70ms) and progress (1s)

## Threading Model
- **Main thread**: rumps event loop, all UI modifications
- **Background threads**: URL resolution (`_resolve_and_play`), decoder subprocess I/O, serial playback worker
- **`_pending_ui` flag**: background threads set this to `"play"` or `"stopped"`, the viz timer (main thread) picks it up and does actual UI work. This was added to fix a crash from modifying autolayout from background threads.
- PyObjC callbacks, CoreAudio listeners, and mixer taps do not mutate playback state inline; they enqueue work back onto the playback worker.

## Menu Layout
```
Now Playing: Song Title
0:45 ├████●─────────────┤ 3:00
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
- `_seek_to_pct(pct)`: restarts playback with ffmpeg `-ss` flag for time offset
- Progress now tracks the seek target immediately and then continues from the correct offset once playback resumes.
- `-ss` with piped input works but is slow (ffmpeg reads and discards data to reach offset). Could be optimized later with yt-dlp `--download-sections` or extracting direct URL for HTTP range seeks.

## Known Issues / Future Work
- Seek via `-ss` on piped ffmpeg is slow for large offsets
- Route changes still require a brief rebuild/restart; this is correctness-first, not gapless handoff
- First-play caching still uses a second yt-dlp fetch after the listen threshold; there is no tee/single-fetch cache path yet
- No skip/previous buttons (only play/pause + seek submenu)
- No volume control (intentional per user preference)
- Playlist playback is hidden; there is no visible queue/count indicator
- App must be launched from user's terminal (not Claude's) to get proper macOS GUI context

## Dependencies (pyproject.toml)
- rumps, numpy, py2app, pyobjc-framework-AVFoundation
- External: yt-dlp, ffmpeg (must be on PATH)

## Running
```
uv run python yt_bar.py
```

## Recent Changes
1. Fixed background-thread UI crash: all menu modifications now dispatched to main thread via `_pending_ui` flag
2. Idle icon changed to `⠆⣿⠰`
3. Removed visible queue UI and `-30s` / `+30s` controls while keeping playlists auto-advancing internally
4. Fixed per-track progress to stay aligned after seek and refreshed the progress bar styling
5. Replaced `sounddevice` / PortAudio with a native `AVAudioEngine` backend, plus CoreAudio default-output listening and engine-configuration rebuilds
6. Stereometer now reads from an `AVAudioEngine` mixer tap instead of the old output stream write loop
7. Added `songs/`-backed offline caching plus a `Recent` submenu for cached video and playlist replay
8. Removed the in-app launch-at-login toggle and documented manual LaunchAgent setup in the README instead
