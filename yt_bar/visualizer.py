import threading

import numpy as np

from .constants import (
    BRAILLE_BASE,
    DOT_BITS,
    GRID_H,
    GRID_W,
    VISUALIZER_SNAPSHOT_FRAMES,
)


class StereometerController:
    def __init__(self):
        self._lock = threading.RLock()
        self._dot_grid = np.zeros((GRID_W, GRID_H), dtype=np.float32)
        self._dot_decay = 0.75
        self._rms_peak = 0.001
        self._snapshot = None

    def dot_grid(self):
        with self._lock:
            if self._snapshot is not None:
                self._compute_stereometer(self._snapshot)
                self._snapshot = None
            return self._dot_grid.copy()

    def reset(self):
        with self._lock:
            self._dot_grid = np.zeros((GRID_W, GRID_H), dtype=np.float32)
            self._rms_peak = 0.001
            self._snapshot = None

    def capture_buffer(self, *, session_id, current_session_id, buffer):
        if session_id != current_session_id():
            return

        stereo = self._stereo_snapshot_from_buffer(buffer)
        if stereo is None:
            return

        with self._lock:
            if session_id == current_session_id():
                self._snapshot = stereo

    @staticmethod
    def _stereo_snapshot_from_buffer(buffer):
        try:
            frame_length = min(int(buffer.frameLength()), VISUALIZER_SNAPSHOT_FRAMES)
            if frame_length <= 0:
                return None

            channel_data = buffer.floatChannelData()
            left = np.frombuffer(
                channel_data[0].as_buffer(frame_length),
                dtype=np.float32,
                count=frame_length,
            ).copy()
            right = np.frombuffer(
                channel_data[1].as_buffer(frame_length),
                dtype=np.float32,
                count=frame_length,
            ).copy()
            return np.column_stack((left, right))
        except Exception:
            return None

    def _compute_stereometer(self, stereo):
        left = stereo[:, 0]
        right = stereo[:, 1]

        mid = (left + right) * 0.5
        side = (left - right) * 0.5

        rms = float(np.sqrt(np.mean(mid**2 + side**2)))
        if rms > self._rms_peak:
            self._rms_peak = rms
        else:
            self._rms_peak *= 0.999
        self._rms_peak = max(self._rms_peak, 0.001)
        scale = 0.9 / self._rms_peak

        mid_scaled = mid * scale
        side_scaled = side * scale
        self._dot_grid *= self._dot_decay

        energy = mid**2 + side**2
        step = max(1, len(mid) // 80)
        indices = np.argsort(energy)[::-1][: len(mid) // step]

        for idx in indices:
            m = float(np.clip(mid_scaled[idx], -1, 1))
            s = float(np.clip(side_scaled[idx], -1, 1))
            x = int((s + 1.0) * 0.5 * (GRID_W - 1) + 0.5)
            y = int((m + 1.0) * 0.5 * (GRID_H - 1) + 0.5)
            x = max(0, min(GRID_W - 1, x))
            y = max(0, min(GRID_H - 1, y))
            self._dot_grid[x, y] = min(1.0, self._dot_grid[x, y] + 0.15)


def grid_to_braille(grid):
    chars = []
    for char_col in range(0, GRID_W, 2):
        code = BRAILLE_BASE
        for local_col in range(2):
            x = char_col + local_col
            if x >= GRID_W:
                continue
            for y in range(GRID_H):
                if grid[x, y] > 0.18:
                    code |= DOT_BITS[local_col][y]
        chars.append(chr(code))
    return "".join(chars)
