import numpy as np

from yt_bar.av_session import AVAudioGraphController
from yt_bar.models import PlaybackSession, PlayRequest


class AllocInitMixin:
    @classmethod
    def alloc(cls):
        return cls()

    def init(self):
        return self


class FakeObserver(AllocInitMixin):
    def initWithCallback_(self, callback):
        self.callback = callback
        return self


class FakeNotificationCenter:
    def __init__(self):
        self.add_calls = []
        self.removed = []

    def addObserver_selector_name_object_(self, observer, selector, name, obj):
        self.add_calls.append((observer, selector, name, obj))

    def removeObserver_(self, observer):
        self.removed.append(observer)


class FakeChannel:
    def __init__(self, frame_capacity):
        self.storage = bytearray(frame_capacity * 4)

    def as_buffer(self, frame_count):
        return memoryview(self.storage)[: frame_count * 4]


class FakePCMBuffer(AllocInitMixin):
    def initWithPCMFormat_frameCapacity_(self, audio_format, frame_capacity):
        self.audio_format = audio_format
        self.frame_capacity = frame_capacity
        self.channels = [FakeChannel(frame_capacity), FakeChannel(frame_capacity)]
        self.frame_length = 0
        return self

    def floatChannelData(self):
        return self.channels

    def setFrameLength_(self, frame_length):
        self.frame_length = frame_length


class FakeAVAudioFormat(AllocInitMixin):
    def initStandardFormatWithSampleRate_channels_(self, sample_rate, channels):
        self.sample_rate = sample_rate
        self.channels = channels
        return self


class FakePlayerTime:
    def __init__(self, sample_time, *, valid=True):
        self._sample_time = sample_time
        self._valid = valid

    def isSampleTimeValid(self):
        return self._valid

    def sampleTime(self):
        return self._sample_time


class FakeAVAudioPlayerNode(AllocInitMixin):
    def __init__(self):
        self.play_calls = 0
        self.pause_calls = 0
        self.stop_calls = 0
        self.schedule_calls = []
        self.render_time = None
        self.player_time = None

    def scheduleBuffer_completionCallbackType_completionHandler_(
        self,
        buffer,
        callback_type,
        completion_handler,
    ):
        self.schedule_calls.append((buffer, callback_type, completion_handler))

    def play(self):
        self.play_calls += 1

    def pause(self):
        self.pause_calls += 1

    def stop(self):
        self.stop_calls += 1

    def lastRenderTime(self):
        return self.render_time

    def playerTimeForNodeTime_(self, render_time):
        return self.player_time


class FakeMixer:
    def __init__(self):
        self.output_bus = None
        self.tap_args = None
        self.removed_bus = None

    def outputFormatForBus_(self, bus):
        self.output_bus = bus
        return "tap-format"

    def installTapOnBus_bufferSize_format_block_(self, bus, buffer_size, tap_format, block):
        self.tap_args = (bus, buffer_size, tap_format, block)

    def removeTapOnBus_(self, bus):
        self.removed_bus = bus


class FakeAVAudioEngine(AllocInitMixin):
    def init(self):
        self.attached = []
        self.connected = []
        self.output_requested = False
        self.prepare_calls = 0
        self.start_calls = 0
        self.stop_calls = 0
        self.mixer = FakeMixer()
        return self

    def attachNode_(self, player):
        self.attached.append(player)

    def mainMixerNode(self):
        return self.mixer

    def connect_to_format_(self, player, mixer, audio_format):
        self.connected.append((player, mixer, audio_format))

    def outputNode(self):
        self.output_requested = True
        return object()

    def prepare(self):
        self.prepare_calls += 1

    def startAndReturnError_(self, error):
        self.start_calls += 1
        return True

    def stop(self):
        self.stop_calls += 1


class FakeAVFoundation:
    AVAudioEngine = FakeAVAudioEngine
    AVAudioPlayerNode = FakeAVAudioPlayerNode
    AVAudioFormat = FakeAVAudioFormat
    AVAudioPCMBuffer = FakePCMBuffer
    AVAudioEngineConfigurationChangeNotification = "engine-config"
    AVAudioPlayerNodeCompletionDataPlayedBack = 99


def make_session():
    return PlaybackSession(
        id=7,
        request=PlayRequest(url="https://example.test/audio", duration=120.0),
    )


def make_controller(notification_center):
    return AVAudioGraphController(
        avfoundation=FakeAVFoundation,
        notification_center=notification_center,
        observer_cls=FakeObserver,
    )


def test_start_builds_graph_registers_observer_and_installs_visualizer_tap():
    notification_center = FakeNotificationCenter()
    controller = make_controller(notification_center)
    session = make_session()
    route_events = []
    visualizer_buffers = []

    controller.start(
        session,
        on_route_event=lambda: route_events.append("route"),
        on_visualizer_buffer=lambda buffer: visualizer_buffers.append(buffer),
    )

    assert session.graph.engine is not None
    assert session.graph.player is not None
    assert session.graph.mixer is session.graph.engine.mixer
    assert session.graph.format.sample_rate == 48000.0
    assert session.graph.format.channels == 2
    assert session.graph.engine.prepare_calls == 1
    assert session.graph.engine.start_calls == 1
    assert notification_center.add_calls[0][1] == "handleConfigChange:"
    assert notification_center.add_calls[0][2] == "engine-config"

    session.graph.notification_observer.callback()
    assert route_events == ["route"]

    tap_block = session.graph.tap_block
    tap_block("buffer", None)
    assert visualizer_buffers == ["buffer"]


def test_schedule_chunk_builds_planar_pcm_buffer_and_schedules_completion():
    notification_center = FakeNotificationCenter()
    controller = make_controller(notification_center)
    session = make_session()
    controller.start(
        session,
        on_route_event=lambda: None,
        on_visualizer_buffer=lambda buffer: None,
    )
    chunk = np.array([[0.25, -0.5], [0.75, -1.0]], dtype=np.float32)
    completions = []

    buffer = controller.schedule_chunk(
        session, chunk, lambda callback_type: completions.append(callback_type)
    )

    scheduled_buffer, callback_type, completion_handler = session.graph.player.schedule_calls[-1]
    assert scheduled_buffer is buffer
    assert callback_type == 99
    assert buffer.frame_length == 2
    assert np.allclose(
        np.frombuffer(buffer.channels[0].storage, dtype=np.float32, count=2),
        [0.25, 0.75],
    )
    assert np.allclose(
        np.frombuffer(buffer.channels[1].storage, dtype=np.float32, count=2),
        [-0.5, -1.0],
    )

    completion_handler(5)
    assert completions == [5]


def test_rendered_frames_uses_valid_player_time_and_clamps_negative_values():
    controller = make_controller(FakeNotificationCenter())
    session = make_session()
    controller.start(
        session,
        on_route_event=lambda: None,
        on_visualizer_buffer=lambda buffer: None,
    )

    assert controller.rendered_frames(session) is None

    session.graph.player.render_time = object()
    session.graph.player.player_time = FakePlayerTime(321)
    assert controller.rendered_frames(session) == 321

    session.graph.player.player_time = FakePlayerTime(-10)
    assert controller.rendered_frames(session) == 0

    session.graph.player.player_time = FakePlayerTime(10, valid=False)
    assert controller.rendered_frames(session) is None


def test_discard_removes_tap_unregisters_observer_and_stops_graph():
    notification_center = FakeNotificationCenter()
    controller = make_controller(notification_center)
    session = make_session()
    controller.start(
        session,
        on_route_event=lambda: None,
        on_visualizer_buffer=lambda buffer: None,
    )
    engine = session.graph.engine
    player = session.graph.player
    mixer = session.graph.mixer
    observer = session.graph.notification_observer

    controller.play(session)
    controller.pause(session)
    controller.prepare(session)
    controller.stop_player(session)
    controller.discard(session)

    assert player.play_calls == 1
    assert player.pause_calls == 1
    assert player.stop_calls == 2
    assert engine.prepare_calls == 2
    assert engine.stop_calls == 1
    assert mixer.removed_bus == 0
    assert notification_center.removed == [observer]
    assert session.graph.engine is None
    assert session.graph.player is None
    assert session.graph.mixer is None
    assert session.graph.format is None
    assert session.graph.notification_observer is None
    assert session.graph.tap_block is None
