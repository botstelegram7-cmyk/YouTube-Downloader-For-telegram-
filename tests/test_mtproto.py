"""The MTProto upload transport: what it is, when it is chosen, and how it behaves.

No network and no credentials are involved: the Pyrogram client is replaced by a fake. What
is pinned down here is the architecture — that the transport is authorised by the *bot token*
alone (no user session anywhere), that it is chosen only when the Bot API cannot carry the
file, and that progress and cancellation both work.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from telegram.error import BadRequest

from bot.download import DownloadResult
from bot.errors import UploadCancelled, UploadError
from bot.ffmpeg import MediaProbe
from bot.mtproto import MtprotoUploader
from bot.upload import send_media
from config import MB, load_settings

TOKEN = "123456789:" + "A" * 35
API_KEY = {"TELEGRAM_API_ID": "1234567", "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef"}

WITH_MT = load_settings({"BOT_TOKEN": TOKEN, **API_KEY})
WITHOUT_MT = load_settings({"BOT_TOKEN": TOKEN})


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
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
    )


class FakeUploader:
    """Stands in for the Pyrogram bot client."""

    def __init__(self) -> None:
        self.ready = True
        self.sent: list[dict] = []
        self.fail_with: Exception | None = None

    def reason(self) -> str:
        return ""

    @property
    def capability_mb(self) -> int:
        return 2000

    async def send(self, path: Path, **kwargs):
        from bot.mtproto import UploadedHandle

        self.sent.append({"path": path, **kwargs})
        if self.fail_with is not None:
            raise self.fail_with
        return UploadedHandle(
            chat_id=kwargs["chat_id"],
            message_id=4242,
            size_bytes=path.stat().st_size,
            file_id="mt-file-id",
            kind=kwargs.get("kind", "video"),
            filename=path.name,
        )


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[str] = []

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


# -- the authentication model ------------------------------------------------- #
def test_without_the_api_key_pair_the_transport_is_off_and_says_what_is_missing():
    uploader = MtprotoUploader(WITHOUT_MT)
    assert uploader.ready is False
    reason = uploader.reason()
    assert "TELEGRAM_API_ID" in reason and "TELEGRAM_API_HASH" in reason
    assert "No phone number or login code" in reason


def test_with_the_api_key_pair_the_transport_is_ready_with_no_session_string():
    uploader = MtprotoUploader(WITH_MT)
    assert uploader.ready is True
    assert uploader.reason() == ""
    assert uploader.capability_mb == 2000
    assert uploader.capability_bytes == 2000 * MB


async def test_sending_without_credentials_fails_before_any_network_call():
    with pytest.raises(UploadError):
        await MtprotoUploader(WITHOUT_MT).send(Path("/nonexistent.mp4"), chat_id=1, kind="video")


async def test_the_client_is_built_for_a_bot_and_writes_no_session_file(monkeypatch):
    """The whole point: bot token + api_id/api_hash, in-memory, no phone, no session file."""
    captured: dict = {}

    class FakeClient:
        def __init__(self, **kwargs) -> None:
            captured.update(kwargs)

        async def start(self):
            return self

        async def stop(self):
            return None

    import pyrogram

    monkeypatch.setattr(pyrogram, "Client", FakeClient)
    uploader = MtprotoUploader(WITH_MT)
    await uploader.start()

    assert captured["bot_token"] == TOKEN
    assert captured["api_id"] == 1234567
    assert captured["api_hash"] == API_KEY["TELEGRAM_API_HASH"]
    assert captured["in_memory"] is True, "no session file may be written to disk"
    assert captured["no_updates"] is True, "the Bot API side owns the conversation"
    assert "session_string" not in captured, "a user session must never be used"


# -- progress and cancellation ------------------------------------------------ #
def test_progress_for_pyrogram_reports_percentage_speed_and_eta():
    uploader = MtprotoUploader(WITH_MT)
    seen = []
    callback = uploader.progress_for_pyrogram(seen.append, None, "video.mp4")

    callback(0, 1000)
    callback(250, 1000)
    callback(1000, 1000)

    assert [round(update.percent) for update in seen] == [0, 25, 100]
    assert all(update.phase == "upload" for update in seen)
    assert seen[-1].speed > 0
    assert "video.mp4" in seen[-1].note


def test_a_cancelled_upload_aborts_from_inside_the_progress_callback():
    uploader = MtprotoUploader(WITH_MT)
    cancel_event = asyncio.Event()
    callback = uploader.progress_for_pyrogram(None, cancel_event, "video.mp4")

    callback(10, 100)  # fine while running

    cancel_event.set()
    with pytest.raises(Exception) as error:
        callback(20, 100)
    assert type(error.value).__name__ == "_Aborted"


# -- routing: small files stay on the Bot API --------------------------------- #
async def test_small_files_go_through_the_bot_api_even_when_mtproto_is_ready(tmp_path):
    path = tmp_path / "small.mp4"
    path.write_bytes(b"\x00" * 4096)
    bot, uploader = FakeBot(), FakeUploader()

    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=4 * MB),
        settings=WITH_MT,
        uploader=uploader,
    )

    assert outcome.method == "sendVideo"
    assert "send_video" in bot.calls
    assert uploader.sent == [], "a small file must not wake MTProto"


async def test_big_files_go_through_mtproto_and_are_sent_straight_to_the_user(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)
    bot, uploader = FakeBot(), FakeUploader()

    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=900 * MB),
        settings=WITH_MT,
        uploader=uploader,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
    )

    assert outcome.via_mtproto is True
    assert outcome.method == "mtproto-video"
    assert len(uploader.sent) == 1
    sent = uploader.sent[0]
    assert sent["chat_id"] == 42, "the bot sends directly to the user — no relay or copy step"
    assert sent["kind"] == "video"
    assert bot.calls == [], "the Bot API is not involved at all"


async def test_a_file_beyond_telegram_s_own_ceiling_is_refused_with_telegram_s_number(tmp_path):
    path = tmp_path / "huge.mp4"
    path.write_bytes(b"\x00" * 4096)
    uploader = FakeUploader()
    uploader.fail_with = UploadError("above Telegram's 2000 MB ceiling")

    with pytest.raises(UploadError):
        await send_media(
            bot=FakeBot(),  # type: ignore[arg-type]
            chat_id=42,
            result=media_result(path, size=2500 * MB),
            settings=WITH_MT,
            uploader=uploader,
        )


async def test_a_broken_mtproto_route_does_not_fail_a_small_file(tmp_path):
    """If the large-file transport is down, a file that fits the Bot API still gets delivered."""
    path = tmp_path / "small.mp4"
    path.write_bytes(b"\x00" * 4096)
    bot = FakeBot()
    uploader = FakeUploader()
    uploader.fail_with = UploadError("connection refused")
    settings = load_settings({"BOT_TOKEN": TOKEN, **API_KEY, "UPLOAD_TRANSPORT": "mtproto"})

    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=4 * MB),
        settings=settings,
        uploader=uploader,
    )

    assert outcome.method == "sendVideo"
    assert "send_video" in bot.calls


async def test_a_big_file_with_no_mtproto_configured_explains_the_two_settings(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)

    with pytest.raises(UploadError) as error:
        await send_media(
            bot=FakeBot(),  # type: ignore[arg-type]
            chat_id=42,
            result=media_result(path, size=600 * MB),
            settings=WITHOUT_MT,
            uploader=None,
        )

    reason = error.value.reason
    assert "TELEGRAM_API_ID" in reason and "TELEGRAM_API_HASH" in reason
    assert "no user account" in reason.lower()


async def test_cancellation_during_the_upload_propagates(tmp_path):
    path = tmp_path / "big.mp4"
    path.write_bytes(b"\x00" * 4096)
    uploader = FakeUploader()
    uploader.fail_with = UploadCancelled()

    with pytest.raises(UploadCancelled):
        await send_media(
            bot=FakeBot(),  # type: ignore[arg-type]
            chat_id=42,
            result=media_result(path, size=600 * MB),
            settings=WITH_MT,
            uploader=uploader,
        )


async def test_bot_api_container_failure_still_falls_back_to_a_document(tmp_path, monkeypatch):
    """The Bot API path keeps its old safety net."""
    path = tmp_path / "small.mp4"
    path.write_bytes(b"\x00" * 4096)

    class PickyBot(FakeBot):
        async def send_video(self, **kwargs):
            self.calls.append("send_video")
            raise BadRequest("Bad Request: VIDEO_FILE_INVALID")

    bot = PickyBot()
    outcome = await send_media(
        bot=bot,  # type: ignore[arg-type]
        chat_id=42,
        result=media_result(path, size=4 * MB),
        settings=WITHOUT_MT,
        uploader=None,
    )

    assert outcome.method == "sendDocument"
    assert "send_document" in bot.calls


# -- send_video / send_audio / send_document, with live progress --------------- #
class FakeMedia:
    file_id = "file-id-123"


class FakeMessage:
    id = 777

    def __init__(self, *, video=None, audio=None, document=None) -> None:
        self.video = video
        self.audio = audio
        self.document = document


class RecordingClient:
    """A Pyrogram client that records which send_* method was used."""

    def __init__(self, *, emit_progress: bool = True) -> None:
        self.calls: list[tuple[str, dict]] = []
        self.emit_progress = emit_progress

    async def send_video(self, **kwargs):
        return self._record("send_video", kwargs, video=FakeMedia(), audio=None, document=None)

    async def send_audio(self, **kwargs):
        return self._record("send_audio", kwargs, video=None, audio=FakeMedia(), document=None)

    async def send_document(self, **kwargs):
        return self._record("send_document", kwargs, video=None, audio=None, document=FakeMedia())

    def _record(self, name, kwargs, *, video, audio, document):
        self.calls.append((name, kwargs))
        progress = kwargs.get("progress")
        if self.emit_progress and progress is not None:
            progress(0, 900)  # Telegram's callback signature: (current, total)
            progress(450, 900)
            progress(900, 900)
        return FakeMessage(video=video, audio=audio, document=document)


def ready_uploader(tmp_path: Path, client) -> MtprotoUploader:
    uploader = MtprotoUploader(WITH_MT)
    uploader._client = client
    uploader._state = MtprotoUploader.READY
    return uploader


def media_file(tmp_path: Path, name: str = "clip.mp4", size: int = 2048) -> Path:
    path = tmp_path / name
    path.write_bytes(b"\x00" * size)
    return path


async def test_video_files_go_out_with_send_video(tmp_path):
    client = RecordingClient()
    uploader = ready_uploader(tmp_path, client)
    seen: list = []

    handle = await uploader.send(
        media_file(tmp_path),
        chat_id=42,
        kind="video",
        caption="<b>A video</b>",
        duration=12.5,
        width=1280,
        height=720,
        source_url="https://www.youtube.com/watch?v=abcdefghijk",
        on_progress=seen.append,
    )

    name, kwargs = client.calls[0]
    assert name == "send_video"
    assert kwargs["supports_streaming"] is True, "Telegram must be able to stream it while downloading"
    assert (kwargs["width"], kwargs["height"]) == (1280, 720)
    assert kwargs["duration"] == 12
    assert kwargs["chat_id"] == 42, "the bot sends straight to the user's chat"
    assert kwargs["video"].endswith("clip.mp4")
    assert kwargs["reply_markup"] is not None, "the source link travels with the file"
    assert handle.message_id == 777 and handle.file_id == "file-id-123"

    # progress_for_pyrogram bridged Telegram's byte counts into the status line
    assert [round(u.percent) for u in seen] == [0, 50, 100]
    assert all(u.phase == "upload" for u in seen)
    assert seen[-1].eta is not None and seen[-1].speed > 0


async def test_audio_files_go_out_with_send_audio(tmp_path):
    client = RecordingClient()
    uploader = ready_uploader(tmp_path, client)

    await uploader.send(
        media_file(tmp_path, "track.m4a"),
        chat_id=42,
        kind="audio",
        title="A song",
        artist="An artist",
        duration=200,
    )

    name, kwargs = client.calls[0]
    assert name == "send_audio"
    assert kwargs["performer"] == "An artist"
    assert kwargs["title"] == "A song"


async def test_anything_else_goes_out_as_a_document(tmp_path):
    client = RecordingClient()
    uploader = ready_uploader(tmp_path, client)

    await uploader.send(media_file(tmp_path, "archive.bin"), chat_id=42, kind="document")

    name, kwargs = client.calls[0]
    assert name == "send_document"
    assert kwargs["force_document"] is True


async def test_a_file_above_telegram_s_ceiling_is_refused_before_the_upload(tmp_path):
    client = RecordingClient()
    uploader = ready_uploader(tmp_path, client)
    uploader.settings = WITH_MT  # 2000 MB ceiling

    oversized = media_file(tmp_path, "huge.mp4", size=2048)
    # Pretend the file is 2.5 GB without writing 2.5 GB: the check reads st_size.
    import os

    os.truncate(oversized, 2500 * MB)

    with pytest.raises(UploadError) as error:
        await uploader.send(oversized, chat_id=42, kind="video")

    assert "2000 MB" in error.value.reason
    assert client.calls == [], "nothing is uploaded when Telegram would reject it anyway"


# -- the connection state machine --------------------------------------------- #
async def test_a_failed_connection_is_recorded_and_leaves_nothing_half_open(monkeypatch):
    stopped = {"count": 0}

    class BrokenClient:
        def __init__(self, **kwargs) -> None:
            pass

        async def start(self):
            raise RuntimeError("unreachable data centre")

        async def stop(self):
            stopped["count"] += 1

    import pyrogram

    monkeypatch.setattr(pyrogram, "Client", BrokenClient)
    uploader = MtprotoUploader(WITH_MT)

    with pytest.raises(UploadError) as error:
        await uploader.start()

    assert uploader.state == MtprotoUploader.ERROR
    assert uploader._client is None
    assert "unreachable data centre" in uploader.last_error
    assert "TELEGRAM_API_ID" in error.value.reason
    assert stopped["count"] == 1, "the half-open client is closed again"

    # /limits and /health must never promise 2000 MB while that is true.
    assert uploader.state in ("error", "off")


async def test_state_is_off_without_credentials_and_idle_before_connecting():
    assert MtprotoUploader(WITHOUT_MT).state == MtprotoUploader.OFF
    assert MtprotoUploader(WITH_MT).state == MtprotoUploader.IDLE
