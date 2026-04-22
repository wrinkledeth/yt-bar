import os
import re

INTERNAL_SAMPLE_RATE = 48000
CHANNELS = 2
PCM_BUFFER_FRAMES = 4096
PCM_BYTES_PER_FRAME = CHANNELS * 4
SCHEDULE_AHEAD_SECONDS = 0.5
SCHEDULE_AHEAD_FRAMES = int(INTERNAL_SAMPLE_RATE * SCHEDULE_AHEAD_SECONDS)
WORKER_TICK_SECONDS = 0.05
ROUTE_CHANGE_DEBOUNCE_SECONDS = 0.25
ROUTE_RETRY_DELAYS = (0.35, 1.0)
PROGRESS_BAR_WIDTH = 22
VISUALIZER_SNAPSHOT_FRAMES = 256
VISUALIZER_TAP_BUFFER_FRAMES = 1024
DECODER_QUEUE_BUFFERS = 24
CACHE_DELAY_SECONDS = 10.0
CACHE_WORKER_COUNT = 2
RECENT_TITLE_LIMIT = 55
SEEK_TRACE_LOGGING = os.environ.get("YT_BAR_SEEK_TRACE", "").strip().lower() in {
    "1",
    "true",
    "yes",
    "on",
}
PARTIAL_CACHE_SUFFIX = ".partial.opus"
PACKAGE_ROOT = os.path.dirname(os.path.abspath(__file__))
APP_ROOT = os.path.dirname(PACKAGE_ROOT)
SONGS_DIR_NAME = "songs"
SONGS_DIR = os.path.join(APP_ROOT, SONGS_DIR_NAME)
RECENT_INDEX_PATH = os.path.join(SONGS_DIR, "recent.json")
SETTINGS_PATH = os.path.join(SONGS_DIR, "settings.json")
MEDIA_PLAYER_FRAMEWORK_PATH = "/System/Library/Frameworks/MediaPlayer.framework"
SKIP_INTERVAL_PRESETS = (10.0, 15.0, 30.0, 60.0, 90.0)
RECENT_SIZE_PRESETS = (5, 10, 20, 30)
DEFAULT_SKIP_INTERVAL_SECONDS = 30.0
DEFAULT_RECENT_MENU_LIMIT = 10

# Stereometer grid: 3 braille chars wide (6 cols) x 4 rows = 6x4 dot grid
GRID_W = 6  # dot columns (3 braille chars x 2 cols each)
GRID_H = 4  # dot rows per braille char

BRAILLE_BASE = 0x2800
DOT_BITS = [
    [0x40, 0x04, 0x02, 0x01],  # col 0 (left): bottom to top
    [0x80, 0x20, 0x10, 0x08],  # col 1 (right): bottom to top
]
PAUSE_TITLE = "⠀⠶⠀"
SAFE_CACHE_KEY_RE = re.compile(r"[^A-Za-z0-9._-]+")
