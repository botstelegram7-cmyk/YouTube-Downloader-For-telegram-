"""FFmpeg helpers, exercised against real media files."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from bot import ffmpeg
from bot.errors import CommandError
from bot.process import ProcessRegistry
from config import Settings, load_settings

#: Honours FFMPEG_BINARY/FFPROBE_BINARY so the suite also runs where FFmpeg is
#: installed outside PATH (containers, Termux, the end-to-end harness).
FFMPEG = os.getenv("FFMPEG_BINARY") or shutil.which("ffmpeg")
FFPROBE = os.getenv("FFPROBE_BINARY") or shutil.which("ffprobe")

pytestmark = pytest.mark.skipif(
    not FFMPEG or not FFPROBE,
    reason="FFmpeg/ffprobe are not installed on this machine",
)

SETTINGS = load_settings(
    {
        "BOT_TOKEN": "123456789:" + "A" * 35,
        "FFMPEG_BINARY": FFMPEG or "ffmpeg",
        "FFPROBE_BINARY": FFPROBE or "ffprobe",
    }
)


def make_video(directory: Path, *, faststart: bool = True, seconds: int = 3) -> Path:
    target = directory / ("fast.mp4" if faststart else "slow.mp4")
    command = [
        FFMPEG,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"testsrc=size=320x240:rate=15:duration={seconds}",
        "-f",
        "lavfi",
        "-i",
        f"sine=frequency=440:duration={seconds}",
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-pix_fmt",
        "yuv420p",
        "-c:a",
        "aac",
        "-shortest",
    ]
    if faststart:
        command += ["-movflags", "+faststart"]
    command.append(str(target))
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode != 0:
        pytest.skip(f"could not build a test video: {completed.stderr[-200:]}")
    return target


def test_probe_reports_real_geometry(tmp_path):
    video = make_video(tmp_path)
    probe = asyncio.run(ffmpeg.probe_media(video, SETTINGS))

    assert probe.width == 320
    assert probe.height == 240
    assert probe.has_video is True
    assert probe.has_audio is True
    assert probe.duration == pytest.approx(3.0, abs=0.5)
    assert probe.size > 1000
    assert "1920" not in probe.resolution
    assert probe.resolution.startswith("320×240")


def test_faststart_detection_and_remux(tmp_path):
    fast = make_video(tmp_path, faststart=True)
    slow = make_video(tmp_path, faststart=False)

    assert ffmpeg.has_faststart(fast) is True
    assert ffmpeg.has_faststart(slow) is False

    async def fix() -> tuple[Path, bool]:
        registry = ProcessRegistry()
        return await ffmpeg.ensure_faststart(slow, SETTINGS, registry=registry)

    path, moved = asyncio.run(fix())
    assert moved is True
    assert ffmpeg.has_faststart(path) is True


def test_thumbnail_is_a_small_jpeg(tmp_path):
    video = make_video(tmp_path)
    target = tmp_path / "thumb.jpg"

    async def build():
        registry = ProcessRegistry()
        return await ffmpeg.make_thumbnail(video, target, SETTINGS, duration=3.0, registry=registry)

    result = asyncio.run(build())

    assert result is not None and result.exists()
    assert result.stat().st_size <= ffmpeg.THUMBNAIL_LIMIT_BYTES
    with result.open("rb") as handle:
        assert handle.read(2) == b"\xff\xd8", "must be a JPEG"


def test_thumbnail_respects_the_width_cap(tmp_path):
    video = make_video(tmp_path)
    target = tmp_path / "thumb.jpg"

    async def build():
        return await ffmpeg.make_thumbnail(video, target, SETTINGS, duration=3.0)

    result = asyncio.run(build())
    probe = asyncio.run(ffmpeg.probe_media(result, SETTINGS))
    assert probe.width <= SETTINGS.thumbnail_max_side


def test_audio_conversion_produces_a_playable_m4a(tmp_path):
    video = make_video(tmp_path)
    target = tmp_path / "audio.m4a"

    async def convert():
        return await ffmpeg.convert_audio(video, target, SETTINGS)

    result = asyncio.run(convert())

    assert result is not None and result.exists()
    probe = asyncio.run(ffmpeg.probe_media(result, SETTINGS))
    assert probe.has_audio is True and probe.has_video is False
    assert probe.audio_codec == "aac"


def test_a_missing_binary_is_reported_clearly(tmp_path):
    broken = Settings(**{**SETTINGS.__dict__, "ffmpeg_path": "definitely-not-ffmpeg"})

    with pytest.raises(Exception) as error:
        asyncio.run(ffmpeg.ensure_available(broken))

    assert "FFmpeg" in str(error.value)


def test_ffprobe_failure_surfaces_as_an_error(tmp_path):
    junk = tmp_path / "broken.mp4"
    junk.write_bytes(b"this is not a video")

    with pytest.raises(CommandError):
        asyncio.run(ffmpeg.probe_media(junk, SETTINGS))


def test_direct_url_inspection_reads_remote_geometry(tmp_path):
    """ffprobe can read a URL — that is how direct links get their real size."""
    import asyncio
    import functools
    import http.server
    import socketserver
    import threading

    from bot.probe import inspect_direct_url

    make_video(tmp_path)

    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(tmp_path))
    handler.log_message = lambda *args, **kwargs: None

    with socketserver.TCPServer(("127.0.0.1", 0), handler) as server:
        port = server.server_address[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            details = asyncio.run(inspect_direct_url(f"http://127.0.0.1:{port}/fast.mp4", SETTINGS))
        finally:
            server.shutdown()

    assert details is not None
    assert details["height"] == 240 and details["width"] == 320
    assert details["vcodec"] != "none" and details["acodec"] != "none"
    assert details["filesize"] > 1000
