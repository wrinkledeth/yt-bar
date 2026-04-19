import Foundation
import objc

from .utils import log_exception


class EngineConfigurationObserver(Foundation.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(EngineConfigurationObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def handleConfigChange_(self, notification):
        callback = getattr(self, "_callback", None)
        if callback:
            callback()


class RecentMenuObserver(Foundation.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(RecentMenuObserver, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    def menuWillOpen_(self, menu):
        callback = getattr(self, "_callback", None)
        if callback:
            callback()


class RemoteCommandBridge(Foundation.NSObject):
    def initWithOwner_(self, owner):
        self = objc.super(RemoteCommandBridge, self).init()
        if self is None:
            return None
        self._owner = owner
        return self

    @objc.python_method
    def _dispatch(self, method_name):
        owner = getattr(self, "_owner", None)
        if owner is None:
            return 0
        handler = getattr(owner, method_name, None)
        if handler is None:
            return 0
        try:
            return int(handler())
        except Exception as exc:
            log_exception(f"Remote command failed: {method_name}", exc)
            return int(owner._remote_command_status_command_failed())

    @objc.typedSelector(b"q@:@")
    def handlePlayCommand_(self, event):
        return self._dispatch("_handle_remote_play_command")

    @objc.typedSelector(b"q@:@")
    def handlePauseCommand_(self, event):
        return self._dispatch("_handle_remote_pause_command")

    @objc.typedSelector(b"q@:@")
    def handleTogglePlayPauseCommand_(self, event):
        return self._dispatch("_handle_remote_toggle_command")

    @objc.typedSelector(b"q@:@")
    def handleSkipForwardCommand_(self, event):
        return self._dispatch("_handle_remote_skip_forward_command")

    @objc.typedSelector(b"q@:@")
    def handleSkipBackwardCommand_(self, event):
        return self._dispatch("_handle_remote_skip_backward_command")

    @objc.typedSelector(b"q@:@")
    def handleNextTrackCommand_(self, event):
        return self._dispatch("_handle_remote_skip_forward_command")

    @objc.typedSelector(b"q@:@")
    def handlePreviousTrackCommand_(self, event):
        return self._dispatch("_handle_remote_skip_backward_command")


class CommonModeTimerTarget(Foundation.NSObject):
    def initWithCallback_(self, callback):
        self = objc.super(CommonModeTimerTarget, self).init()
        if self is None:
            return None
        self._callback = callback
        return self

    @objc.typedSelector(b"v@:@")
    def fire_(self, timer):
        try:
            self._callback(timer)
        except Exception as exc:
            log_exception("common-mode timer", exc)


def schedule_common_mode_timer(interval, callback):
    target = CommonModeTimerTarget.alloc().initWithCallback_(callback)
    timer = Foundation.NSTimer.alloc().initWithFireDate_interval_target_selector_userInfo_repeats_(
        Foundation.NSDate.date(),
        interval,
        target,
        b"fire:",
        None,
        True,
    )
    Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(
        timer, Foundation.NSRunLoopCommonModes
    )
    return timer, target
