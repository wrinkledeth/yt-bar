from dataclasses import dataclass

import objc

from .constants import MEDIA_PLAYER_FRAMEWORK_PATH
from .utils import log_exception

MP_REMOTE_COMMAND_STATUS_SUCCESS = 0
MP_REMOTE_COMMAND_STATUS_NO_SUCH_CONTENT = 100
MP_REMOTE_COMMAND_STATUS_COMMAND_FAILED = 200


@dataclass(frozen=True)
class MediaPlayerSupport:
    command_center_class: object
    now_playing_info_center_class: object
    command_status_success: int
    command_status_command_failed: int
    command_status_no_such_content: int
    property_elapsed_playback_time: str
    property_playback_rate: str
    property_title: str
    property_playback_duration: str


def load_media_player_support():
    try:
        bundle = objc.loadBundle(
            "MediaPlayer",
            globals(),
            bundle_path=MEDIA_PLAYER_FRAMEWORK_PATH,
        )
        variables = {}
        objc.loadBundleVariables(
            bundle,
            variables,
            [
                ("MPNowPlayingInfoPropertyElapsedPlaybackTime", b"@"),
                ("MPNowPlayingInfoPropertyPlaybackRate", b"@"),
                ("MPMediaItemPropertyTitle", b"@"),
                ("MPMediaItemPropertyPlaybackDuration", b"@"),
            ],
        )
        return MediaPlayerSupport(
            command_center_class=objc.lookUpClass("MPRemoteCommandCenter"),
            now_playing_info_center_class=objc.lookUpClass("MPNowPlayingInfoCenter"),
            command_status_success=MP_REMOTE_COMMAND_STATUS_SUCCESS,
            command_status_command_failed=MP_REMOTE_COMMAND_STATUS_COMMAND_FAILED,
            command_status_no_such_content=MP_REMOTE_COMMAND_STATUS_NO_SUCH_CONTENT,
            property_elapsed_playback_time=variables["MPNowPlayingInfoPropertyElapsedPlaybackTime"],
            property_playback_rate=variables["MPNowPlayingInfoPropertyPlaybackRate"],
            property_title=variables["MPMediaItemPropertyTitle"],
            property_playback_duration=variables["MPMediaItemPropertyPlaybackDuration"],
        )
    except Exception as exc:
        log_exception("MediaPlayer integration unavailable", exc)
        return None
