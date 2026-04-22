from .media_player import load_media_player_support
from .models import UICommand
from .objc_bridges import RemoteCommandBridge
from .utils import log_exception


class RemoteCommandController:
    def __init__(
        self,
        *,
        engine,
        enqueue_ui_action,
        current_track,
        current_track_snapshot,
        skip_interval,
    ):
        self._engine = engine
        self._enqueue_ui_action = enqueue_ui_action
        self._current_track = current_track
        self._current_track_snapshot = current_track_snapshot
        self._skip_interval = skip_interval
        self._support = None
        self._remote_command_center = None
        self._now_playing_info_center = None
        self._remote_command_bridge = None
        self._remote_commands: list[object] = []
        self._setup_media_player()

    def update_skip_intervals(self):
        center = self._remote_command_center
        if center is None:
            return
        interval = self._skip_interval()
        center.skipForwardCommand().setPreferredIntervals_([interval])
        center.skipBackwardCommand().setPreferredIntervals_([interval])

    def sync_now_playing_info(self):
        support = self._support
        center = self._now_playing_info_center
        if support is None or center is None:
            return

        track = self._current_track()
        if track is None or not self._engine.is_active:
            self.clear_now_playing_info()
            return

        info = {
            support.property_title: track.title,
            support.property_elapsed_playback_time: float(self._engine.elapsed),
            support.property_playback_rate: 0.0 if self._engine.is_paused else 1.0,
        }
        duration = self._engine.duration or track.duration
        if duration > 0:
            info[support.property_playback_duration] = float(duration)

        try:
            center.setNowPlayingInfo_(info)
        except Exception as exc:
            log_exception("Failed to update now playing info", exc)

    def clear_now_playing_info(self):
        center = self._now_playing_info_center
        if center is None:
            return
        try:
            center.setNowPlayingInfo_(None)
        except Exception as exc:
            log_exception("Failed to clear now playing info", exc)

    def close(self):
        self.clear_now_playing_info()
        self._unregister_remote_commands()

    def _remote_command_status_success(self):
        support = self._support
        return 0 if support is None else support.command_status_success

    def _remote_command_status_command_failed(self):
        support = self._support
        return 0 if support is None else support.command_status_command_failed

    def _remote_command_status_no_such_content(self):
        support = self._support
        return 0 if support is None else support.command_status_no_such_content

    def _handle_remote_play_command(self):
        current_index, track = self._current_track_snapshot()
        if self._engine.is_active:
            if self._engine.is_paused:
                self._enqueue_ui_action(UICommand.play())
            return self._remote_command_status_success()
        if track is None or current_index < 0:
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action(UICommand.play())
        return self._remote_command_status_success()

    def _handle_remote_pause_command(self):
        if not self._engine.is_active:
            return self._remote_command_status_no_such_content()
        if not self._engine.is_paused:
            self._enqueue_ui_action(UICommand.pause())
        return self._remote_command_status_success()

    def _handle_remote_toggle_command(self):
        current_index, track = self._current_track_snapshot()
        if not self._engine.is_active and (track is None or current_index < 0):
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action(UICommand.toggle())
        return self._remote_command_status_success()

    def _handle_remote_skip_forward_command(self):
        if not self._engine.is_active or self._engine.duration <= 0:
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action(UICommand.seek_delta(self._skip_interval()))
        return self._remote_command_status_success()

    def _handle_remote_skip_backward_command(self):
        if not self._engine.is_active or self._engine.duration <= 0:
            return self._remote_command_status_no_such_content()
        self._enqueue_ui_action(UICommand.seek_delta(-self._skip_interval()))
        return self._remote_command_status_success()

    def _setup_media_player(self):
        support = load_media_player_support()
        if support is None:
            return

        try:
            self._support = support
            self._remote_command_center = support.command_center_class.sharedCommandCenter()
            self._now_playing_info_center = support.now_playing_info_center_class.defaultCenter()
            self._remote_command_bridge = RemoteCommandBridge.alloc().initWithOwner_(self)
            self._register_remote_commands()
        except Exception as exc:
            log_exception("Failed to initialize MediaPlayer integration", exc)
            self._unregister_remote_commands()
            self._support = None
            self._remote_command_center = None
            self._now_playing_info_center = None
            self._remote_command_bridge = None

    def _register_remote_commands(self):
        center = self._remote_command_center
        bridge = self._remote_command_bridge
        if center is None or bridge is None:
            return

        commands = [
            (center.playCommand(), "handlePlayCommand:"),
            (center.pauseCommand(), "handlePauseCommand:"),
            (center.togglePlayPauseCommand(), "handleTogglePlayPauseCommand:"),
            (center.skipForwardCommand(), "handleSkipForwardCommand:"),
            (center.skipBackwardCommand(), "handleSkipBackwardCommand:"),
            (center.nextTrackCommand(), "handleNextTrackCommand:"),
            (center.previousTrackCommand(), "handlePreviousTrackCommand:"),
        ]
        self.update_skip_intervals()
        center.nextTrackCommand().setEnabled_(True)
        center.previousTrackCommand().setEnabled_(True)

        for command, selector in commands:
            command.addTarget_action_(bridge, selector)
            self._remote_commands.append(command)

    def _unregister_remote_commands(self):
        bridge = self._remote_command_bridge
        if bridge is None:
            self._remote_commands.clear()
            return
        for command in self._remote_commands:
            try:
                command.removeTarget_(bridge)
            except Exception:
                pass
        self._remote_commands.clear()
