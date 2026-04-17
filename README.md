# yt-bar

![](image.png)

yt-bar makes YouTube feel more like a real music player. It:
	•	lives in the macOS menu bar
	•	loads tracks from the clipboard
	•	supports media keys
	•	saves tracks locally for offline playback

## Install

Requirements:
- macOS
- Python `3.12+`
- `uv`
- `yt-dlp` on `PATH`
- `ffmpeg` on `PATH`

Install the Python env:

```bash
uv sync
```

Run it once directly:

```bash
.venv/bin/python yt_bar.py
```

Or install it as a LaunchAgent so it starts cleanly in your logged-in GUI session:

```bash
./install.sh
```

Remove the LaunchAgent later with:

```bash
./uninstall.sh
```

## Usage

Core: 
- Copy a YouTube video or playlist URL and use `Play from Clipboard`.
- Playback starts streaming immediately and the track gets saved locally on listen.
- F7 / F8 / F9 media keys to play/pause & skip forward and back. 
- `Recent` allows for offline playback of recent tracks. (Hold `Option` in that menu to delete recent tracks)

Additional: 
- The badge next to the song title shows playback source: `◌` means streaming, `●` means local cache.
- `Settings` lets you toggle `Compact Menu`, change skip interval seconds, and change the max recent-list size.

## Visualizer Algorithm
The visualizer is a tiny stereometer rendered as `3` braille characters in the menu bar. 

It reads a short stereo snapshot from the `AVAudioEngine` mixer tap, converts left/right into mid/side, and plots the strongest samples into a fading `6 x 4` dot grid. 

In practice this means:
- narrow / mono material forms a tighter center trace
- wide stereo material splays outward
- phasey or side-heavy material pushes farther toward the edges

Enjoy :)