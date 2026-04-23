import numpy as np

from yt_bar.visualizer import StereometerController


class FakeChannel:
    def __init__(self, samples):
        self.storage = np.asarray(samples, dtype=np.float32).tobytes()

    def as_buffer(self, frame_count):
        return memoryview(self.storage)[: frame_count * 4]


class FakeBuffer:
    def __init__(self, left, right):
        self._left = FakeChannel(left)
        self._right = FakeChannel(right)
        self._frame_length = min(len(left), len(right))

    def frameLength(self):
        return self._frame_length

    def floatChannelData(self):
        return [self._left, self._right]


def test_capture_buffer_updates_grid_for_current_session_and_returns_copy():
    visualizer = StereometerController()
    buffer = FakeBuffer([0.9, 0.7, -0.5, -0.9], [0.9, 0.7, -0.5, -0.9])

    visualizer.capture_buffer(
        session_id=7,
        current_session_id=lambda: 7,
        buffer=buffer,
    )

    first = visualizer.dot_grid()
    second = visualizer.dot_grid()

    assert first.sum() > 0
    first[0, 0] = 99.0
    assert second[0, 0] != 99.0


def test_capture_buffer_ignores_non_current_session():
    visualizer = StereometerController()
    buffer = FakeBuffer([1.0, 0.5], [1.0, 0.5])

    visualizer.capture_buffer(
        session_id=7,
        current_session_id=lambda: 8,
        buffer=buffer,
    )

    assert np.array_equal(visualizer.dot_grid(), np.zeros((6, 4), dtype=np.float32))


def test_capture_buffer_rechecks_session_before_storing_snapshot():
    visualizer = StereometerController()
    buffer = FakeBuffer([1.0, 0.5], [1.0, 0.5])
    session_ids = iter([7, 8])

    visualizer.capture_buffer(
        session_id=7,
        current_session_id=lambda: next(session_ids),
        buffer=buffer,
    )

    assert np.array_equal(visualizer.dot_grid(), np.zeros((6, 4), dtype=np.float32))


def test_reset_clears_grid_and_pending_snapshot_state():
    visualizer = StereometerController()
    buffer = FakeBuffer([0.8, -0.8], [0.8, -0.8])

    visualizer.capture_buffer(
        session_id=3,
        current_session_id=lambda: 3,
        buffer=buffer,
    )
    visualizer.reset()

    assert np.array_equal(visualizer.dot_grid(), np.zeros((6, 4), dtype=np.float32))
