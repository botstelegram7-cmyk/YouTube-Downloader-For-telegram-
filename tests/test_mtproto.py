"""The MTProto upload path: when it is used, and how it reports progress.

No network is touched. The Pyrofork client is replaced by a fake, which is enough to pin
the behaviour that matters: the transport decision, the progress bridge, and the fact that
a delivery failure is reported instead of silently dropping the file.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telegram.error import BadRequest

from bot.download import DownloadResult
from bot.errors import UploadCancelled, UploadError
from bot.ffmpeg import MediaProbe
from bot.mtproto import MtprotoUploader, UploadedHandle
from bot.upload import send_media
from config import load_settings

TOKEN = "123456789:" + "A" * 35
SESSION = {
    "TELEGRAM_API_ID": "1234567",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELEGRAM_SESSION_STRING": "BQ" + "x" * 40,
}

WITH_SESSION = load_settings({"BOT_TOKEN": TOKEN, **SESSION})
WITHOUT_SESSION = load_settings({"BOT_TOKEN": TOKEN})


def media_result(path: Path, *, size: int) -> DownloadResult:
    probe = MediaProbe(
        path=path,
        duration=12.5,
        width=1280,
        height=720,
        size=size,
        has_video=True,
        has_audio=True,
        video_codec="h264",
        audio_codec="aac",
        container="mp4",
    )
    return DownloadResult(
        path=path,
        title="Test video",
        duration=12.5,
        width=1280,
        height=720,
        size=size,
        kind="video",
        quality_label="720p",
        thumbnail=None,
        faststart=True,
        probe=probe,
        artist="",
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
    )


class FakeUploader:
    """Stands in for the Pyrofork client."""

    def __init__(self) -> None:
        self.ready = True
        self.uploaded: list[dict] = []
        self.deleted: list[UploadedHandle] = []
        self.fail_with: Exception | None = None

    async def upload(self, path: Path, **kwargs) -> UploadedHandle:
        self.uploaded.append({"path": path, **kwargs})
        if self.fail_with is not None:
            raise self.fail_with
        return UploadedHandle(
            chat_id=123456789,
            message_id=4242,
            size_bytes=path.stat().st_size,
            file_id="session-file-id",
            account_id=999,
            kind=kwargs.get("kind", "video"),
            filename=path.name,
        )

    async def delete(self, handle: UploadedHandle) -> None:
        self.deleted.append(handle)


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.copy_error: Exception | None = None

    async def send_chat_action(self, **kwargs) -> None:
        self.calls.append("send_chat_action")

    async def send_video(self, **kwargs):
        self.calls.append("send_video")
        return object()

    async def send_audio(self, **kwargs):
        self.calls.append("send_audio")
        return object()

    async def send_document(self, **kwargs):
        self.calls.append("send_document")
        return object()

    async def copy_message(self, **kwargs):
        self.calls.append("copy_message")
        if self.copy_error is not None:
            raise self.copy_error
        return None

    async def forward_message(self, **kwargs):
        self.calls.append("forward_message")
        return None


# -- availability ------------------------------------------------------------ #
def test_missing_credentials_are_named_one_by_one():
    uploader = MtprotoUploader(WITHOUT_SESSION)
    assert uploader.ready is False
    reason = uploader.reason()
    assert "TELEGRAM_API_ID" in reason
    assert "TELEGRAM_API_HASH" in reason
    assert "TELEGRAM_SESSION_STRING" in reason
    assert "make_session.py" in reason


def test_a_complete_session_is_ready():
    uploader = MtprotoUploader(WITH_SESSION)
    assert uploader.ready is True
    assert uploader.reason() == ""
    assert uploader.upload_cap_mb == 2000


async def test_uploading_without_credentials_fails_before_any_network_call():
    with pytest.raises(UploadError):
        await MtprotoUploader(WITHOUT_SESSION).upload(Path("/nonexistent.mp4"), kind="video")


# -- progress and cancellation ----------------------------------------------- #
def test_progress_is_bridged_with_percentage_and_speed():
    uploader = MtprotoUploader(WITH_SESSION)
    seen = []
    callback = uploader._progress(seen.append, None, "video.mp4")

    callback(0, 1000)
    callback(250, 1000)
    callback(1000, 1000)

    assert [round(update.percent) for update in seen] == [0, 25, 100]
    assert all(update.phase == "upload" for update in seen)
    assert seen[-1].speed > 0
    assert "video.mp4" in seen[-1].note


def test_a_cancelled_upload_aborts_from_inside_the_progress_callback():
    uploader = MtprotoUploader(WITH_SESSION)
    cancel_event = asyncio.Event()
    callback = uploader._progress(None, cancel_event, "video.mp4")

    callback(10, 100)  # fine while running

    cancel_event.set()
    with pytest.raises(Exception) as error:  # _Aborted, kept private on purpose
        callback(20, 100)
    assert type(error.value).__name__ == "_Aborted"


# -- routing ----------------------------------------------------------------- #
async def test_small_files_still_go_through_the_bot_api(tmp_path):
    path = tmp_path / "small.mp4"
    path.write_bytes(b"\x00" * 4096)
    bot, uploader = FakeBot(), FakeUploader()

    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=4 * 1024 * 1024),
        settings=WITH_SESSION,
        uploader=uploader,
    )

    assert outcome.method == "sendVideo"
    assert "send_video" in bot.calls
    assert uploader.uploaded == [], "a small file must not wake the session"


async def test_big_files_travel_through_the_session_and_are_copied(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)
    bot, uploader = FakeBot(), FakeUploader()

    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=600 * 1024 * 1024),
        settings=WITH_SESSION,
        uploader=uploader,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
    )

    assert outcome.method == "mtproto-copy"
    assert outcome.via_mtproto is True
    assert len(uploader.uploaded) == 1
    assert "copy_message" in bot.calls
    assert uploader.deleted, "the storage copy should be cleaned up after delivery"


async def test_a_failed_copy_falls_back_to_forwarding(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)
    bot, uploader = FakeBot(), FakeUploader()
    bot.copy_error = BadRequest("copy refused")

    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=600 * 1024 * 1024),
        settings=WITH_SESSION,
        uploader=uploader,
    )

    assert outcome.method == "mtproto-forward"
    assert "forward_message" in bot.calls


async def test_a_big_file_without_a_session_is_refused_with_advice(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)

    with pytest.raises(UploadError) as error:
        await send_media(
            bot=FakeBot(),  # type: ignore[arg-type]
            chat_id=42,
            result=media_result(path, size=600 * 1024 * 1024),
            settings=WITHOUT_SESSION,
            uploader=None,
        )

    assert "TELEGRAM_SESSION_STRING" in error.value.reason


async def test_cancellation_during_upload_propagates(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)
    uploader = FakeUploader()
    uploader.fail_with = UploadCancelled()

    with pytest.raises(UploadCancelled):
        await send_media(
            bot=FakeBot(),  # type: ignore[arg-type]
            chat_id=42,
            result=media_result(path, size=600 * 1024 * 1024),
            settings=WITH_SESSION,
            uploader=uploader,
        )
