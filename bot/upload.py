"""Uploading to Telegram: real metadata, a thumbnail, a clean caption — at any size.

Two transports, both authorised by the same bot token, chosen per file by what Telegram
will accept:

* **Bot API** (``sendVideo``/``sendAudio``/``sendDocument``) — used whenever the file fits
  the API's 50 MB ceiling. Telegram renders the preview itself.
* **MTProto** (:mod:`bot.mtproto`) — used for anything larger. Same bot, same token, but
  Telegram's protocol ceiling is 2000 MB instead of 50 MB.

There is no size *policy* in this module and none in the configuration: a file is sent
through the transport that can carry it, and if a file is bigger than every available
transport can handle, the message says so in Telegram's own numbers.

Captions, thumbnails and the streaming flag are identical on both paths, so the user cannot
tell which transport carried their file.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot, InputFile
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from bot.download import DownloadResult, ProgressUpdate
from bot.errors import UploadError
from bot.keyboards import delivered_keyboard
from bot.mtproto import MtprotoUploader
from bot.progress import StatusReporter, UploadProgressFile
from bot.texts import done_caption
from bot.utils import human_size, progress_bar, safe_filename
from config import MB, Settings

log = logging.getLogger("bot.upload")

CAPTION_LIMIT = 1000  # Telegram allows 1024; leave room for the source link


@dataclass
class UploadOutcome:
    method: str
    message: object
    thumbnail_attached: bool
    filename: str

    @property
    def as_document(self) -> bool:
        return self.method == "sendDocument"

    @property
    def via_mtproto(self) -> bool:
        return self.method.startswith("mtproto")


def _timeout_for(size_bytes: int) -> float:
    """Generous write timeout: 1.5 s per MB, at least 10 minutes."""
    return max(600.0, size_bytes / MB * 1.5)


def _open_thumbnail(path: Path | None):
    if path is None or not path.exists():
        return None
    return InputFile(path.open("rb"), filename="thumb.jpg")


def _duration(seconds: float) -> str:
    from bot.utils import human_duration

    return human_duration(seconds)


class _UploadProgress:
    """Turns byte counts from the Bot API upload stream into status edits."""

    def __init__(self, reporter: StatusReporter | None, title: str, quality: str) -> None:
        import time

        self.reporter = reporter
        self.title = title
        self.quality = quality
        self._started = time.monotonic()
        self._last = 0.0

    def __call__(self, sent: int, total: int) -> None:
        if self.reporter is None or not total:
            return
        import time

        now = time.monotonic()
        if now - self._last < self.reporter.interval:
            return
        self._last = now
        percent = min(100.0, sent * 100.0 / total)
        speed = sent / max(0.001, now - self._started)
        self.reporter.update_from_thread(_upload_text(self.title, self.quality, percent, sent, total, speed))


def _upload_text(
    title: str,
    quality: str,
    percent: float,
    sent: int,
    total: int | None,
    speed: float | None,
) -> str:
    lines = [
        f"⬆️ <b>Uploading to Telegram</b> — {quality}",
        f"<i>{title}</i>",
        "",
        f"{progress_bar(percent)}  <b>{percent:.1f}%</b>",
    ]
    if total:
        tail = f"  ·  ⚡ {human_size(speed)}/s" if speed else ""
        lines.append(f"📦 {human_size(sent)} / {human_size(total)}{tail}")
    return "\n".join(lines)


def _report(reporter: StatusReporter | None, result: DownloadResult, update: ProgressUpdate) -> None:
    """MTProto progress → status message."""
    if reporter is None:
        return
    reporter.update_from_thread(
        _upload_text(
            result.title,
            result.quality_label,
            update.percent if update.percent is not None else 0.0,
            update.downloaded,
            update.total,
            update.speed,
        )
    )


async def send_media(
    *,
    bot: Bot,
    chat_id: int,
    result: DownloadResult,
    settings: Settings,
    reporter: StatusReporter | None = None,
    reply_to_message_id: int | None = None,
    source_url: str = "",
    uploader: MtprotoUploader | None = None,
    cancel_event=None,
) -> UploadOutcome:
    """Deliver a finished download, using whichever transport can carry it."""
    caption = _caption_with_url(result, source_url)

    bot_api_ok = result.size <= settings.bot_api_capability_mb * MB
    mtproto_ok = uploader is not None and bool(getattr(uploader, "ready", False))
    forced = settings.upload_transport == "mtproto"

    if mtproto_ok and (forced or not bot_api_ok):
        try:
            return await _send_via_mtproto(
                chat_id=chat_id,
                result=result,
                caption=caption,
                uploader=uploader,  # type: ignore[arg-type]
                reporter=reporter,
                reply_to_message_id=reply_to_message_id,
                source_url=source_url,
                cancel_event=cancel_event,
            )
        except UploadError as exc:
            if not bot_api_ok:
                raise
            # The big-file transport is down, but this file is small enough to go the
            # ordinary way — a broken MTProto route must not fail a perfectly good job.
            log.warning("MTProto upload failed (%s); falling back to the Bot API", exc.reason)

    if bot_api_ok:
        return await _send_via_bot_api(
            bot=bot,
            chat_id=chat_id,
            result=result,
            caption=caption,
            reporter=reporter,
            reply_to_message_id=reply_to_message_id,
            source_url=source_url,
        )

    raise UploadError(_cannot_send_message(result, settings))


def _cannot_send_message(result: DownloadResult, settings: Settings) -> str:
    """The one place a file is refused for its size — using Telegram's numbers, not ours."""
    size = human_size(result.size)
    if settings.mtproto_ready:
        return (
            f"This file is {size}, above Telegram's {settings.mtproto_capability_mb} MB ceiling for a bot "
            "over MTProto — the largest any bot can upload. Pick a lower quality, or an audio-only "
            "download for long videos."
        )
    return (
        f"This file is {size}. The Bot API accepts at most {settings.bot_api_capability_mb} MB per upload, "
        "and no setting can raise that: it is Telegram's limit for that API. Set TELEGRAM_API_ID and "
        "TELEGRAM_API_HASH (from https://my.telegram.org) to send up to "
        f"{settings.mtproto_capability_mb} MB with the same bot token — no user account or session string "
        "is involved. A self-hosted Bot API server (TELEGRAM_API_BASE) is the other option."
    )


async def _send_via_bot_api(
    *,
    bot: Bot,
    chat_id: int,
    result: DownloadResult,
    caption: str,
    reporter: StatusReporter | None,
    reply_to_message_id: int | None,
    source_url: str,
) -> UploadOutcome:
    """Upload through Telegram's Bot API — the native path for anything up to 50 MB."""
    timeout = _timeout_for(result.size)
    extension = result.path.suffix.lstrip(".") or "mp4"
    filename = f"{safe_filename(result.title)}.{extension}"
    thumbnail_file = _open_thumbnail(result.thumbnail)
    progress = _UploadProgress(reporter, result.title, result.quality_label)
    reader = UploadProgressFile(result.path, progress)
    video_input = InputFile(reader, filename=filename)

    common = {
        "chat_id": chat_id,
        "caption": caption,
        "parse_mode": ParseMode.HTML,
        "write_timeout": timeout,
        "read_timeout": timeout,
        "reply_to_message_id": reply_to_message_id,
        "reply_markup": delivered_keyboard(source_url or result.source_url),
    }

    try:
        if result.kind == "audio":
            await _chat_action(bot, chat_id, ChatAction.UPLOAD_DOCUMENT)
            message = await bot.send_audio(
                audio=video_input,
                title=result.title[:64],
                performer=(result.artist or "")[:64],
                duration=int(result.duration) or None,
                thumbnail=thumbnail_file,
                **common,
            )
            return UploadOutcome("sendAudio", message, result.thumbnail is not None, filename)

        await _chat_action(bot, chat_id, ChatAction.UPLOAD_VIDEO)
        try:
            message = await bot.send_video(
                video=video_input,
                duration=int(result.duration) or None,
                width=result.width or None,
                height=result.height or None,
                supports_streaming=True,
                thumbnail=thumbnail_file,
                **common,
            )
            return UploadOutcome("sendVideo", message, result.thumbnail is not None, filename)
        except BadRequest as exc:
            if not _is_container_problem(exc):
                raise
            # Telegram will not treat this as video (unusual container/codec):
            # deliver the same file as a document instead of failing the job.
            log.warning("sendVideo refused (%s); falling back to sendDocument", exc)
            await _rewind(reader)
            message = await bot.send_document(
                document=InputFile(reader, filename=filename),
                thumbnail=thumbnail_file,
                **common,
            )
            return UploadOutcome("sendDocument", message, result.thumbnail is not None, filename)
    except RetryAfter as exc:
        raise UploadError(f"Telegram asked me to slow down; try again in {exc.retry_after:.0f}s.") from exc
    except BadRequest as exc:
        raise UploadError(f"Telegram refused the file: {exc.message or exc}") from exc
    except NetworkError as exc:
        raise UploadError(f"The upload was interrupted ({exc}).") from exc
    except TelegramError as exc:
        raise UploadError(f"The upload failed: {exc}") from exc
    finally:
        reader.close()
        if thumbnail_file is not None:
            # Closing the temp thumbnail handle must never mask the real outcome.
            with contextlib.suppress(Exception):
                thumbnail_file.input_file_content.close()  # type: ignore[attr-defined]


async def _send_via_mtproto(
    *,
    chat_id: int,
    result: DownloadResult,
    caption: str,
    uploader: MtprotoUploader,
    reporter: StatusReporter | None,
    reply_to_message_id: int | None,
    source_url: str,
    cancel_event,
) -> UploadOutcome:
    """Send through MTProto: the bot's own session, up to Telegram's 2000 MB ceiling."""
    handle = await uploader.send(
        result.path,
        chat_id=chat_id,
        kind=result.kind,
        caption=caption,
        title=result.title,
        artist=result.artist,
        duration=result.duration,
        width=result.width,
        height=result.height,
        thumbnail=result.thumbnail,
        source_url=source_url or result.source_url,
        reply_to_message_id=reply_to_message_id,
        on_progress=lambda update: _report(reporter, result, update),
        cancel_event=cancel_event,
    )
    log.info("Delivered %s via MTProto (%s)", handle.filename or result.path.name, human_size(handle.size_bytes))
    return UploadOutcome(
        f"mtproto-{handle.kind}",
        None,
        result.thumbnail is not None,
        handle.filename or result.path.name,
    )


def _caption_with_url(result: DownloadResult, source_url: str) -> str:
    kind = "audio" if result.kind == "audio" else "video"
    text = done_caption(
        title=result.title,
        quality=result.quality_label,
        resolution=result.resolution,
        duration=_duration(result.duration),
        size=human_size(result.size),
        url=source_url,
        kind=kind,
    )
    return text[:CAPTION_LIMIT]


async def _rewind(reader: UploadProgressFile) -> None:
    reader.reset()


async def _chat_action(bot: Bot, chat_id: int, action: str) -> None:
    with contextlib.suppress(TelegramError):
        await bot.send_chat_action(chat_id=chat_id, action=action)


def _is_container_problem(exc: BadRequest) -> bool:
    """True when the failure is about the video itself, not the request."""
    text = (getattr(exc, "message", "") or str(exc)).lower()
    markers = (
        "video_file_invalid",
        "wrong file identifier",
        "failed to get http url content",
        "file is not a video",
        "media_empty",
        "video dimensions",
        "can't parse",
    )
    return any(marker in text for marker in markers)
