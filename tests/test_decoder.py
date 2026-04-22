from yt_bar.constants import CHANNELS, INTERNAL_SAMPLE_RATE
from yt_bar.decoder import build_ffmpeg_command, build_ytdlp_command


def test_build_ytdlp_command_streams_best_audio_to_stdout():
    assert build_ytdlp_command("https://youtube.example/watch?v=1") == [
        "yt-dlp",
        "-f",
        "bestaudio",
        "-o",
        "-",
        "--no-warnings",
        "https://youtube.example/watch?v=1",
    ]


def test_build_local_ffmpeg_command_decodes_seeked_pcm():
    command = build_ffmpeg_command("/tmp/song.opus", is_local=True, start_time=12.5)

    assert command[:5] == ["ffmpeg", "-ss", "12.5", "-i", "/tmp/song.opus"]
    assert command[5:] == [
        "-f",
        "f32le",
        "-acodec",
        "pcm_f32le",
        "-ac",
        str(CHANNELS),
        "-ar",
        str(INTERNAL_SAMPLE_RATE),
        "-loglevel",
        "error",
        "-",
    ]


def test_build_stream_ffmpeg_command_reads_pipe_without_seek_when_start_is_zero():
    command = build_ffmpeg_command(
        "https://youtube.example/watch?v=1",
        is_local=False,
        start_time=0,
    )

    assert command[:3] == ["ffmpeg", "-i", "pipe:0"]
    assert "-ss" not in command
