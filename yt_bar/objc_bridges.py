import os

import AppKit
import Foundation
import objc

from .utils import log_exception

_status_item_file_drop_handlers = {}


def _status_item_file_drop_key(button):
    if button is None:
        return None
    try:
        return objc.pyobjc_id(button)
    except Exception:
        return id(button)


def _status_item_file_drop_handler(button):
    return _status_item_file_drop_handlers.get(_status_item_file_drop_key(button))


def _first_dragged_file_path(dragging_info):
    if dragging_info is None:
        return None

    pasteboard = dragging_info.draggingPasteboard()
    if pasteboard is None:
        return None

    options = {AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True}
    urls = pasteboard.readObjectsForClasses_options_([Foundation.NSURL], options) or ()
    for url in urls:
        try:
            path = str(url.path() or "").strip()
        except Exception:
            continue
        if not path or not os.path.isfile(path):
            continue
        return os.path.abspath(path)
    return None


def _status_item_dragging_entered(button, dragging_info):
    if _status_item_file_drop_handler(button) is None:
        return AppKit.NSDragOperationNone
    if _first_dragged_file_path(dragging_info) is None:
        return AppKit.NSDragOperationNone
    return AppKit.NSDragOperationCopy


def _status_item_prepare_drag_operation(button, dragging_info):
    return (
        _status_item_file_drop_handler(button) is not None
        and _first_dragged_file_path(dragging_info) is not None
    )


def _status_item_perform_drag_operation(button, dragging_info):
    handler = _status_item_file_drop_handler(button)
    if handler is None:
        return False

    source_path = _first_dragged_file_path(dragging_info)
    if source_path is None:
        return False

    try:
        handler(source_path)
    except Exception as exc:
        log_exception("status item file drop", exc)
        return False
    return True


def install_status_item_file_drop(button, on_path):
    if button is None:
        return

    # PyObjC proxy objects cannot store arbitrary Python attributes.
    _status_item_file_drop_handlers[_status_item_file_drop_key(button)] = on_path
    button.registerForDraggedTypes_([AppKit.NSPasteboardTypeFileURL])


class NSStatusBarButton(objc.Category(AppKit.NSStatusBarButton)):
    def draggingEntered_(self, dragging_info):
        return _status_item_dragging_entered(self, dragging_info)

    def prepareForDragOperation_(self, dragging_info):
        return _status_item_prepare_drag_operation(self, dragging_info)

    def performDragOperation_(self, dragging_info):
        return _status_item_perform_drag_operation(self, dragging_info)


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
    Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, Foundation.NSRunLoopCommonModes)
    return timer, target


def schedule_default_mode_timer_once(delay, callback):
    target = CommonModeTimerTarget.alloc().initWithCallback_(callback)
    timer = Foundation.NSTimer.alloc().initWithFireDate_interval_target_selector_userInfo_repeats_(
        Foundation.NSDate.dateWithTimeIntervalSinceNow_(float(delay)),
        0.0,
        target,
        b"fire:",
        None,
        False,
    )
    Foundation.NSRunLoop.currentRunLoop().addTimer_forMode_(timer, Foundation.NSDefaultRunLoopMode)
    return timer, target
