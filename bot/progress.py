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

class ProgressCard:
    """A status message that can turn itself into a live animation.

    Telegram lets a *media* message's media be replaced (``editMessageMedia``), which is what
    makes an animated progress card possible: the job starts with a looping "working" GIF and
    the card swaps in the upload GIF when the upload begins, while the caption carries the
    real numbers. Text-only deployments (``RICH_MESSAGES=false``, or no asset available) get
    exactly the text behaviour this class had before — the throttling, the idempotence and
    the "only the first finish counts" rules are shared.
    """

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message: Message,
        *,
        interval: float = 2.0,
        loop: asyncio.AbstractEventLoop | None = None,
        keyboard=None,
        animated: bool = False,
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.message = message
        self.message_id = message.message_id
        self.interval = max(1.0, interval)
        self.loop = loop or asyncio.get_event_loop()
        self.keyboard = keyboard
        self.animated = animated
        self._last_edit = 0.0
        self._last_text = ""
        self._lock = asyncio.Lock()
        self._final = False
        self._switching: set[str] = set()

    @property
    def message_id_current(self) -> int:
        return self.message_id

    # -- text / caption -------------------------------------------------------
    async def update(self, text: str, *, force: bool = False) -> None:
        """Edit the card, unless it is too soon or nothing changed."""
        if self._final or text == self._last_text:
            return

        now = time.monotonic()
        if not force and now - self._last_edit < self.interval:
            return

        async with self._lock:
            if self._final or text == self._last_text:
                return
            try:
                if self.animated:
                    await self.bot.edit_message_caption(
                        chat_id=self.chat_id,
                        message_id=self.message_id,
                        caption=text,
                        parse_mode="HTML",
                        reply_markup=self.keyboard,
                    )
                else:
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
                log.debug("Rate limited while editing the status card; backing off %.1fs", float(exc.retry_after))
            except BadRequest as exc:
                lowered = str(exc).lower()
                if "not modified" not in lowered and "not found" not in lowered:
                    log.debug("Status card edit rejected: %s", exc)
                self._last_text = text
            except TelegramError as exc:
                log.debug("Could not edit the status card: %s", exc)

    def update_from_thread(self, text: str, *, force: bool = False) -> None:
        """Thread-safe variant: safe to call from any thread or task.

        ``force`` bypasses the interval — used for the one update that carries the first
        real numbers, so the speed and the ETA are on screen immediately instead of after
        the first interval has passed.
        """
        with contextlib.suppress(RuntimeError):  # loop already closed (shutdown)
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(self.update(text, force=force)))

    # -- animation ------------------------------------------------------------
    def show_animation(self, asset, caption: str, *, key: str) -> None:
        """Schedule a one-shot switch of the card's media to ``asset``.

        Called when a phase starts. It is idempotent per ``key`` (a retried job must not
        flip back to the download GIF) and it never blocks the job: a card that cannot be
        animated simply keeps editing its text.
        """
        if not self.animated or asset is None or key in self._switching or self._final:
            return
        from telegram import InputFile

        source = self.file_id_for(asset) or InputFile(asset.bytes(), filename=asset.name)
        self._switching.add(key)

        async def switch() -> None:
            from telegram import InputMediaAnimation

            try:
                await self.bot.edit_message_media(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    media=InputMediaAnimation(media=source, caption=caption, parse_mode="HTML"),
                    reply_markup=self.keyboard,
                )
                self._last_text = caption
                self._last_edit = time.monotonic()
            except TelegramError as exc:
                log.debug("Could not switch the status card to %s: %s", asset.name, exc)
                self.animated = False  # stop trying; text edits still work

        with contextlib.suppress(RuntimeError):
            self.loop.call_soon_threadsafe(lambda: asyncio.create_task(switch()))

    def file_id_for(self, asset) -> str | None:
        """The asset's Telegram id, if this card has been told about the library."""
        library = getattr(self, "assets", None)
        return library.file_id(asset.name) if library is not None else None

    def remember_media(self, name: str, message: Message) -> None:
        """Learn the ``file_id`` of the media this card just sent, so it is never re-uploaded."""
        library = getattr(self, "assets", None)
        animation = getattr(message, "animation", None)
        if library is not None and animation is not None:
            library.remember(name, animation.file_id)

    # -- ending ---------------------------------------------------------------
    async def finish(self, text: str | None = None, *, reply_markup=None) -> None:
        """Final edit; only the first call counts, and the buttons are removed."""
        if self._final:
            return
        self._final = True
        if text is None:
            return
        try:
            if self.animated:
                await self.bot.edit_message_caption(
                    chat_id=self.chat_id,
                    message_id=self.message_id,
                    caption=text,
                    parse_mode="HTML",
                    reply_markup=reply_markup,
                )
            else:
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
            log.debug("Could not delete the status message: %s", exc)


#: The plain-text reporter is the single-message card without animation — one implementation,
#: two presentations. Kept as a name because it reads better at the call sites that only want
#: text (and because the tests refer to it).
StatusReporter = ProgressCard
