"""Live progress on one Telegram message, and upload-progress file reading.

Telegram enforces roughly one message per second per chat, so edits are throttled
hard. Progress arrives from the engine's output stream, so every update crosses back
into the event loop with ``call_soon_threadsafe``.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import logging
import time
from collections.abc import Callable
from pathlib import Path

from telegram import Bot, Message
from telegram.constants import ChatAction
from telegram.error import BadRequest, RetryAfter, TelegramError

log = logging.getLogger("bot.progress")


class StatusReporter:
    """Owns one status message and edits it politely."""

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message: Message,
        *,
        interval: float = 2.0,
        loop: asyncio.AbstractEventLoop | None = None,
        keyboard=None,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message = message
        self.message_id = message.message_id
        self.interval = max(1.0, interval)
        self.loop = loop or asyncio.get_event_loop()
        self._last_edit = 0.0
        self._last_text = message.text or ""
        self._lock = asyncio.Lock()
        self._final = False
        self.keyboard = keyboard

    # -- text updates -------------------------------------------------------
    async def update(self, text: str, *, force: bool = False) -> None:
        """Edit the message, unless it is too soon or nothing changed."""
        if self._final or text == self._last_text:
            return

        now = time.monotonic()
        if not force and now - self._last_edit < self.interval:
            return

        async with self._lock:
            if self._final or text == self._last_text:
                return
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    text=text,
                    parse_mode="HTML",
                    disable_web_page_preview=True,
                    reply_markup=self.keyboard,
                )
                self._last_text = text
                self._last_edit = now
            except RetryAfter as exc:
                self._last_edit = time.monotonic() + float(exc.retry_after)
                log.debug("Rate limited while editing status; backing off %.1fs", float(exc.retry_after))
            except BadRequest as exc:
                if "not modified" not in str(exc).lower() and "message to edit not found" not in str(exc).lower():
                    log.debug("Status edit rejected: %s", exc)
                self._last_text = text
            except TelegramError as exc:
                log.debug("Could not edit status message: %s", exc)

    def update_from_thread(self, text: str) -> None:
        """Thread-safe variant: safe to call from any thread or task."""
        with contextlib.suppress(RuntimeError):  # loop already closed (shutdown)
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.update(text)))

    async def finish(self, text: str | None = None, *, reply_markup=None) -> None:
        """Final edit; only the first call counts, and the buttons are removed."""
        if self._final:
            return
        self._final = True
        if text is None:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.chat_id,
                message_id=self.message_id,
                text=text,
                parse_mode="HTML",
                disable_web_page_preview=True,
                reply_markup=reply_markup,
            )
        except TelegramError as exc:
            log.debug("Final status edit failed: %s", exc)

    async def delete(self) -> None:
        """Remove the status message (the delivered file speaks for itself)."""
        self._final = True
        try:
            await self.bot.delete_message(chat_id=self.chat_id, message_id=self.message_id)
        except TelegramError as exc:
            log.debug("Could not delete status message: %s", exc)


class UploadProgressFile:
    """A file wrapper that counts the bytes handed to the HTTP layer.

    python-telegram-bot streams this object straight into the multipart request, so
    counting here measures the real upload instead of guessing. Counting happens in
    ``read``/``readinto`` — the buffered layer must not inflate the number by
    pre-fetching.
    """

    def __init__(self, path: Path, on_bytes: Callable[[int, int], None]) -> None:
        self.path = path
        self.total = path.stat().st_size
        self.sent = 0
        self._on_bytes = on_bytes
        self._handle = path.open("rb")

    # -- accounting ---------------------------------------------------------
    def _count(self, amount: int) -> None:
        self.sent += amount
        try:
            self._on_bytes(self.sent, self.total)
        except Exception as exc:
            log.debug("Upload progress callback failed: %s", exc)

    @property
    def percent(self) -> float:
        if not self.total:
            return 0.0
        return min(100.0, self.sent * 100.0 / self.total)

    def reset(self) -> None:
        """Rewind for a retry: seek to the start and forget the bytes counted."""
        self._handle.seek(0)
        self.sent = 0

    # -- file protocol ------------------------------------------------------
    def read(self, size: int = -1) -> bytes:
        data = self._handle.read(size)
        if data:
            self._count(len(data))
        return data

    def readinto(self, buffer) -> int:
        read = self._handle.readinto(buffer)
        if read:
            self._count(read)
        return read or 0

    def seek(self, offset: int, whence: int = io.SEEK_SET) -> int:
        position = self._handle.seek(offset, whence)
        if offset == 0 and whence == io.SEEK_SET:
            self.sent = 0
        return position

    def tell(self) -> int:
        return self._handle.tell()

    def seekable(self) -> bool:
        return True

    def readable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def fileno(self) -> int:
        return self._handle.fileno()

    def flush(self) -> None:
        return None

    def close(self) -> None:
        with contextlib.suppress(OSError):  # pragma: no cover
            self._handle.close()

    def __enter__(self) -> UploadProgressFile:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __getattr__(self, item: str):
        # Anything else (name, mode, …) is served by the underlying file.
        if item == "_handle":
            raise AttributeError(item)
        return getattr(self._handle, item)


async def typing_action(bot: Bot, chat_id: int, action: str = ChatAction.TYPING) -> None:
    """Best-effort chat action; never raises."""
    with contextlib.suppress(TelegramError):
        await bot.send_chat_action(chat_id=chat_id, action=action)
