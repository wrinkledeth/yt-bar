import pytest

import yt_bar.objc_bridges as objc_bridges


class FakeButton:
    def __init__(self):
        self.drag_types = []

    def registerForDraggedTypes_(self, drag_types):
        self.drag_types.append(list(drag_types))


class FakeURL:
    def __init__(self, path):
        self._path = path

    def path(self):
        return self._path


class FakePasteboard:
    def __init__(self, urls):
        self.urls = list(urls)
        self.read_calls = []

    def readObjectsForClasses_options_(self, classes, options):
        self.read_calls.append((classes, options))
        return list(self.urls)


class FakeDraggingInfo:
    def __init__(self, pasteboard):
        self._pasteboard = pasteboard

    def draggingPasteboard(self):
        return self._pasteboard


@pytest.fixture(autouse=True)
def clear_drop_registry():
    objc_bridges._status_item_file_drop_handlers.clear()
    yield
    objc_bridges._status_item_file_drop_handlers.clear()


def test_install_status_item_file_drop_registers_button_and_handler():
    button = FakeButton()

    def handler(path):
        return path

    objc_bridges.install_status_item_file_drop(button, handler)

    assert button.drag_types == [[objc_bridges.AppKit.NSPasteboardTypeFileURL]]
    assert objc_bridges._status_item_file_drop_handler(button) is handler


def test_status_item_dragging_entered_accepts_valid_local_file(tmp_path):
    button = FakeButton()
    accepted = []
    source = tmp_path / "song.mp3"
    source.write_bytes(b"audio")
    pasteboard = FakePasteboard([FakeURL(str(source))])
    dragging_info = FakeDraggingInfo(pasteboard)

    objc_bridges.install_status_item_file_drop(button, accepted.append)

    assert (
        objc_bridges._status_item_dragging_entered(button, dragging_info)
        == objc_bridges.AppKit.NSDragOperationCopy
    )
    assert objc_bridges._status_item_prepare_drag_operation(button, dragging_info) is True
    assert pasteboard.read_calls[0] == (
        [objc_bridges.Foundation.NSURL],
        {objc_bridges.AppKit.NSPasteboardURLReadingFileURLsOnlyKey: True},
    )
    assert len(pasteboard.read_calls) == 2


def test_status_item_dragging_entered_rejects_missing_or_unregistered_files(tmp_path):
    button = FakeButton()
    source = tmp_path / "missing.mp3"
    dragging_info = FakeDraggingInfo(FakePasteboard([FakeURL(str(source))]))

    assert (
        objc_bridges._status_item_dragging_entered(button, dragging_info)
        == objc_bridges.AppKit.NSDragOperationNone
    )

    def handler(path):
        return path

    objc_bridges.install_status_item_file_drop(button, handler)

    assert (
        objc_bridges._status_item_dragging_entered(button, dragging_info)
        == objc_bridges.AppKit.NSDragOperationNone
    )
    assert objc_bridges._status_item_prepare_drag_operation(button, dragging_info) is False


def test_status_item_perform_drag_operation_dispatches_first_valid_file(tmp_path):
    first = tmp_path / "first.mp3"
    second = tmp_path / "second.mp3"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    button = FakeButton()
    imported_paths = []
    dragging_info = FakeDraggingInfo(FakePasteboard([FakeURL(str(first)), FakeURL(str(second))]))

    objc_bridges.install_status_item_file_drop(button, imported_paths.append)

    assert objc_bridges._status_item_perform_drag_operation(button, dragging_info) is True
    assert imported_paths == [str(first.resolve())]


def test_status_item_perform_drag_operation_ignores_invalid_file_entries(tmp_path):
    button = FakeButton()
    imported_paths = []
    valid = tmp_path / "song.mp3"
    valid.write_bytes(b"audio")
    dragging_info = FakeDraggingInfo(
        FakePasteboard([FakeURL(str(tmp_path / "missing.mp3")), FakeURL(str(valid))])
    )

    objc_bridges.install_status_item_file_drop(button, imported_paths.append)

    assert objc_bridges._status_item_perform_drag_operation(button, dragging_info) is True
    assert imported_paths == [str(valid.resolve())]
