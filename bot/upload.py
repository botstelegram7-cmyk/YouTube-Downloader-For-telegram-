"""Uploading to Telegram: real metadata, a thumbnail, a clean caption.

Videos go out with ``sendVideo`` (inline-playable, streaming enabled) rather than
as documents. A document is only used when Telegram refuses the video container
itself, and that is logged when it happens.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass
from pathlib import Path

from telegram import Bot, InputFile, Message
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError

from bot.download import DownloadResult
from bot.errors import UploadError
from bot.progress import StatusReporter, UploadProgressFile
from bot.texts import done_caption
from bot.utils import human_size, progress_bar, safe_filename
from config import Settings

log = logging.getLogger("bot.upload")

CAPTION_LIMIT = 1000  # Telegram allows 1024; leave room for the join


@dataclass
class UploadOutcome:
    method: str
    message: Message
    thumbnail_attached: bool
    filename: str

    @property
    def as_document(self) -> bool:
        return self.method == "sendDocument"


def _timeout_for(size_bytes: int) -> float:
    """Generous write timeout: 1.5 s per MB, at least 10 minutes."""
    return max(600.0, size_bytes / (1024 * 1024) * 1.5)


def _open_thumbnail(path: Path | None):
    if path is None or not path.exists():
        return None
    return InputFile(path.open("rb"), filename="thumb.jpg")


def _duration(seconds: float) -> str:
    from bot.utils import human_duration

    return human_duration(seconds)


class _UploadProgress:
    """Turns byte counts from the upload stream into status edits."""

    def __init__(self, reporter: StatusReporter | None, title: str, quality: str) -> None:
        self.reporter = reporter
        self.title = title
        self.quality = quality
        import time

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
        elapsed = max(0.001, now - self._started)
        speed = sent / elapsed
        text = (
            f"⬆️ <b>Uploading to Telegram</b> — {self.quality}\n"
            f"<i>{self.title}</i>\n\n"
            f"{progress_bar(percent)}  <b>{percent:.1f}%</b>\n"
            f"📦 {human_size(sent)} / {human_size(total)}  ·  ⚡ {human_size(speed)}/s"
        )
        self.reporter.update_from_thread(text)


async def send_media(
    *,
    bot: Bot,
    chat_id: int,
    result: DownloadResult,
    settings: Settings,
    reporter: StatusReporter | None = None,
    reply_to_message_id: int | None = None,
    source_url: str = "",
) -> UploadOutcome:
    """Deliver a finished download to the chat."""
    caption = _caption_with_url(result, source_url)
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
