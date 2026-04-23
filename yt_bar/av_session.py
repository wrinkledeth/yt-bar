import AVFoundation
import Foundation
import numpy as np

from .constants import (
    CHANNELS,
    INTERNAL_SAMPLE_RATE,
    VISUALIZER_TAP_BUFFER_FRAMES,
)
from .objc_bridges import EngineConfigurationObserver


class AVAudioGraphController:
    def __init__(
        self,
        *,
        avfoundation=AVFoundation,
        notification_center=None,
        observer_cls=EngineConfigurationObserver,
    ):
        self._avfoundation = avfoundation
        self._notification_center = (
            notification_center or Foundation.NSNotificationCenter.defaultCenter()
        )
        self._observer_cls = observer_cls

    def start(self, session, *, on_route_event, on_visualizer_buffer):
        engine = self._avfoundation.AVAudioEngine.alloc().init()
        player = self._avfoundation.AVAudioPlayerNode.alloc().init()
        audio_format = (
            self._avfoundation.AVAudioFormat.alloc().initStandardFormatWithSampleRate_channels_(
                float(INTERNAL_SAMPLE_RATE),
                CHANNELS,
            )
        )

        engine.attachNode_(player)
        mixer = engine.mainMixerNode()
        engine.connect_to_format_(player, mixer, audio_format)
        engine.outputNode()

        session.graph.engine = engine
        session.graph.player = player
        session.graph.mixer = mixer
        session.graph.format = audio_format

        observer = self._observer_cls.alloc().initWithCallback_(on_route_event)
        self._notification_center.addObserver_selector_name_object_(
            observer,
            "handleConfigChange:",
            self._avfoundation.AVAudioEngineConfigurationChangeNotification,
            engine,
        )
        session.graph.notification_observer = observer

        tap_format = mixer.outputFormatForBus_(0)

        def tap_block(buffer, _when):
            on_visualizer_buffer(buffer)

        mixer.installTapOnBus_bufferSize_format_block_(
            0,
            VISUALIZER_TAP_BUFFER_FRAMES,
            tap_format,
            tap_block,
        )
        session.graph.tap_block = tap_block

        engine.prepare()
        engine.startAndReturnError_(None)

    @staticmethod
    def prepare(session):
        if session.graph.engine is not None:
            session.graph.engine.prepare()

    @staticmethod
    def play(session):
        if session.graph.player is not None:
            session.graph.player.play()

    @staticmethod
    def pause(session):
        if session.graph.player is not None:
            session.graph.player.pause()

    @staticmethod
    def stop_player(session):
        if session.graph.player is not None:
            session.graph.player.stop()

    def schedule_chunk(self, session, chunk, completion_handler):
        buffer = self._make_pcm_buffer(session.graph.format, chunk)
        session.graph.player.scheduleBuffer_completionCallbackType_completionHandler_(
            buffer,
            self._avfoundation.AVAudioPlayerNodeCompletionDataPlayedBack,
            completion_handler,
        )
        return buffer

    @staticmethod
    def rendered_frames(session):
        if session.graph.player is None:
            return None

        render_time = session.graph.player.lastRenderTime()
        if render_time is None:
            return None

        player_time = session.graph.player.playerTimeForNodeTime_(render_time)
        if player_time is None or not player_time.isSampleTimeValid():
            return None

        return max(0, int(player_time.sampleTime()))

    def discard(self, session):
        if session.graph.mixer is not None:
            try:
                session.graph.mixer.removeTapOnBus_(0)
            except Exception:
                pass

        if session.graph.notification_observer is not None:
            try:
                self._notification_center.removeObserver_(session.graph.notification_observer)
            except Exception:
                pass

        if session.graph.player is not None:
            try:
                session.graph.player.stop()
            except Exception:
                pass

        if session.graph.engine is not None:
            try:
                session.graph.engine.stop()
            except Exception:
                pass

        session.graph.engine = None
        session.graph.player = None
        session.graph.mixer = None
        session.graph.format = None
        session.graph.notification_observer = None
        session.graph.tap_block = None

    def _make_pcm_buffer(self, audio_format, interleaved_chunk):
        frame_count = int(len(interleaved_chunk))
        buffer = self._avfoundation.AVAudioPCMBuffer.alloc().initWithPCMFormat_frameCapacity_(
            audio_format,
            frame_count,
        )
        left = np.ascontiguousarray(interleaved_chunk[:, 0], dtype=np.float32)
        right = np.ascontiguousarray(interleaved_chunk[:, 1], dtype=np.float32)

        channel_data = buffer.floatChannelData()
        left_dest = np.frombuffer(
            channel_data[0].as_buffer(frame_count),
            dtype=np.float32,
            count=frame_count,
        )
        right_dest = np.frombuffer(
            channel_data[1].as_buffer(frame_count),
            dtype=np.float32,
            count=frame_count,
        )
        left_dest[:] = left
        right_dest[:] = right
        buffer.setFrameLength_(frame_count)
        return buffer
