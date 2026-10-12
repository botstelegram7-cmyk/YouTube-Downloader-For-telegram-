"""Telegram's MTProto API as an upload transport — authorised with the *bot token*.

Why this exists
---------------
The HTTP Bot API caps a bot's uploads at 50 MB. MTProto, the protocol every official client
speaks, allows **2000 MB per file for bots** (4000 MB is a Premium *user* privilege, which a
bot cannot have). Pyrogram can sign in as a bot, so the ceiling can be raised without a
personal account, a phone number, a login code or a session string:

* ``api_id`` / ``api_hash`` identify the *application* (from https://my.telegram.org);
* ``BOT_TOKEN`` identifies the *account* — the same token the rest of the bot already uses.

That distinction matters: api_id/api_hash alone authenticate nothing. Pyrogram needs all
three, and with them it authorises over MTProto directly, with no phone step.

Because this client *is* the bot, the file is sent straight to the user's chat — no relay
chat, no ``copy_message``, no second pass over the network.

Sending follows the standard Pyrogram recipe: :meth:`pyrogram.Client.send_video` for videos
(so Telegram treats them as playable, streamable videos), :meth:`send_audio` for audio tracks
and :meth:`send_document` for anything else, each driven by :func:`progress_for_pyrogram`,
which reports percentage, transferred bytes, speed and ETA while an upload runs and aborts it
when the user cancels.

Everything degrades politely: without credentials the uploader explains why it is off, and
:mod:`bot.upload` falls back to the Bot API paths.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from bot.download import ProgressUpdate
from bot.errors import UploadCancelled, UploadError
from config import MB, VERSION, Settings

log = logging.getLogger("bot.mtproto")


class _Aborted(Exception):
    """Raised inside Pyrogram's progress callback to stop an upload at once."""


@dataclass
class UploadedHandle:
    """A file that MTProto has delivered."""

    chat_id: int
    message_id: int
    size_bytes: int
    file_id: str
    kind: str  # "video" | "audio" | "document"
    filename: str = ""


class MtprotoUploader:
    """A Pyrogram *bot* client used only for sending files.

    One client serves every job: connecting is expensive and Telegram counts sessions, so it
    is started once at boot and stopped at shutdown. It reads no updates (``no_updates``) —
    the Bot API side of the bot owns the conversation.
    """

    #: Connection states reported by :attr:`state` and shown by ``/health`` and ``/limits``.
    OFF, IDLE, CONNECTING, READY, ERROR = "off", "idle", "connecting", "ready", "error"

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self._lock = asyncio.Lock()
        self._me: dict | None = None
        self._state = self.IDLE if self.ready else self.OFF
        self._last_error = ""

    @property
    def state(self) -> str:
        """Where the transport is right now — informational, never a gate."""
        return self._state

    @property
    def last_error(self) -> str:
        return self._last_error

    # -- availability -------------------------------------------------------
    def reason(self) -> str:
        """Why MTProto is unusable, or ``""`` when it is ready."""
        missing = []
        if not self.settings.api_id:
            missing.append("TELEGRAM_API_ID")
        if not self.settings.api_hash:
            missing.append("TELEGRAM_API_HASH")
        if not self.settings.bot_token:
            missing.append("BOT_TOKEN")
        if missing:
            return (
                "MTProto upload is not configured (missing "
                + ", ".join(missing)
                + "). Get the two API values from https://my.telegram.org/ → API development tools; "
                "BOT_TOKEN is the token you already use. No phone number or login code is involved."
            )
        return ""

    @property
    def ready(self) -> bool:
        return not self.reason()

    @property
    def capability_mb(self) -> int:
        """Telegram's per-file ceiling for a bot over MTProto."""
        return self.settings.mtproto_capability_mb

    @property
    def capability_bytes(self) -> int:
        return self.capability_mb * MB

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> bool:
        """Authorise and connect. Returns True when the transport is usable."""
        if not self.ready:
            log.info("MTProto upload unavailable: %s", self.reason())
            return False

        from pyrogram import Client

        async with self._lock:
            if self._client is not None:
                return True
            self._state = self.CONNECTING
            client = Client(
                name="ytbot-uploader",  # never written to disk: the session lives in memory
                api_id=self.settings.api_id,
                api_hash=self.settings.api_hash,
                bot_token=self.settings.bot_token,
                in_memory=True,  # nothing to persist, nothing to leak
                no_updates=True,  # the Bot API side owns updates
                max_concurrent_transmissions=self.settings.mt_connections,
                device_model="YouTube Downloader Bot",
                app_version=VERSION,
            )
            try:
                await client.start()  # signs in as the bot; no phone, no code
            except Exception as exc:
                self._state = self.ERROR
                self._last_error = str(exc)
                # Leave nothing half-open behind: a later attempt must start from scratch.
                with contextlib.suppress(Exception):
                    await client.stop()
                log.error("MTProto could not authorise the bot: %s", exc)
                raise UploadError(
                    "Telegram refused the MTProto connection. Check TELEGRAM_API_ID / TELEGRAM_API_HASH "
                    f"(they must belong to the same app you registered) and the outbound connection to "
                    f"Telegram's data centres. Details: {exc}"
                ) from exc
            self._client = client
            self._state = self.READY
            self._last_error = ""
            log.info(
                "MTProto uploads enabled: bot session authorised, up to %s MB per file",
                self.capability_mb,
            )
            return True

    async def stop(self) -> None:
        client, self._client = self._client, None
        self._me = None
        self._state = self.IDLE if self.ready else self.OFF
        if client is None:
            return
        try:
            await client.stop()
            log.info("MTProto session closed")
        except Exception as exc:
            log.debug("MTProto session did not close cleanly: %s", exc)

    async def _ensure_client(self):
        if self._client is None:
            await self.start()
        if self._client is None:
            raise UploadError(self.reason() or "MTProto is not available.")
        return self._client

    async def identity(self) -> dict:
        """Who the session is: the bot itself, with the ceiling that applies to it."""
        if self._me is not None:
            return self._me
        client = await self._ensure_client()
        me = await client.get_me()
        self._me = {
            "id": me.id,
            "username": me.username or "",
            "name": me.first_name or "",
            "capability_mb": self.capability_mb,
        }
        log.info(
            "MTProto session is @%s (id %s) — ceiling %s MB per file",
            self._me["username"] or "-",
            self._me["id"],
            self.capability_mb,
        )
        return self._me

    # -- sending ------------------------------------------------------------
    def progress_for_pyrogram(self, on_progress, cancel_event, filename: str):
        """Build the ``progress(current, total)`` callback Pyrogram calls while uploading.

        This is the usual Pyrogram upload-progress recipe, with two differences that suit a
        bot: the numbers are handed to :class:`bot.progress.StatusReporter`, which owns the
        single status message and throttles the edits to Telegram's ~1 edit/second, and a
        cancelled job raises from inside the callback, which aborts the upload immediately
        instead of after the whole file has been sent.

        :param on_progress: sink for :class:`bot.download.ProgressUpdate` (percentage,
            transferred bytes, speed, ETA).
        :param cancel_event: the job's :class:`asyncio.Event`; set means "stop now".
        :param filename: shown in the status line.
        """
        started = time.monotonic()

        def callback(current: int, total: int, *args) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise _Aborted()
            if on_progress is None:
                return
            elapsed = max(0.001, time.monotonic() - started)
            speed = current / elapsed
            on_progress(
                ProgressUpdate(
                    phase="upload",
                    percent=min(100.0, current * 100.0 / total) if total else None,
                    downloaded=current,
                    total=total or None,
                    speed=speed,
                    eta=((total - current) / speed) if (total and speed > 0) else None,
                    note=f"Uploading {filename}…",
                )
            )

        return callback

    def _markup(self, url: str):
        """A Pyrogram ``Watch on YouTube`` button (the Bot API equivalent lives in keyboards)."""
        if not url:
            return None
        from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        return InlineKeyboardMarkup([[InlineKeyboardButton("▶️ Watch on YouTube", url=url)]])

    async def send(
        self,
        path: Path,
        *,
        chat_id: int,
        kind: str,
        caption: str = "",
        title: str = "",
        artist: str = "",
        duration: float = 0.0,
        width: int = 0,
        height: int = 0,
        thumbnail: Path | None = None,
        source_url: str = "",
        reply_to_message_id: int | None = None,
        on_progress=None,
        cancel_event: asyncio.Event | None = None,
    ) -> UploadedHandle:
        """Upload one file with MTProto and send it to ``chat_id``."""
        if not self.ready:
            raise UploadError(self.reason())

        size = path.stat().st_size
        if size > self.capability_bytes:
            raise UploadError(
                f"This file is {size / MB:.0f} MB, above Telegram's {self.capability_mb} MB ceiling for a "
                "bot over MTProto. Nothing in this application lowers that number — only Telegram sets it."
            )

        client = await self._ensure_client()
        thumb = str(thumbnail) if thumbnail is not None and Path(thumbnail).is_file() else None
        progress = self.progress_for_pyrogram(on_progress, cancel_event, path.name)
        markup = self._markup(source_url)
        html = _parse_mode()

        from pyrogram.errors import FloodWait, RPCError

        try:
            if kind == "video":
                message = await client.send_video(
                    chat_id=chat_id,
                    video=str(path),
                    caption=caption,
                    parse_mode=html,
                    thumb=thumb,
                    duration=int(duration) or None,
                    width=width or None,
                    height=height or None,
                    file_name=path.name,
                    supports_streaming=True,  # Telegram can stream it while it downloads
                    disable_notification=False,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=markup,
                    progress=progress,
                )
            elif kind == "audio":
                message = await client.send_audio(
                    chat_id=chat_id,
                    audio=str(path),
                    caption=caption,
                    parse_mode=html,
                    thumb=thumb,
                    duration=int(duration) or None,
                    performer=(artist or "")[:64] or None,
                    title=(title or path.stem)[:64],
                    file_name=path.name,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=markup,
                    progress=progress,
                )
            else:
                message = await client.send_document(
                    chat_id=chat_id,
                    document=str(path),
                    caption=caption,
                    parse_mode=html,
                    thumb=thumb,
                    file_name=path.name,
                    force_document=True,
                    reply_to_message_id=reply_to_message_id,
                    reply_markup=markup,
                    progress=progress,
                )
        except _Aborted as exc:
            raise UploadCancelled() from exc
        except FloodWait as exc:
            raise UploadError(f"Telegram asked me to wait {exc.value}s before uploading again.") from exc
        except RPCError as exc:
            message = getattr(exc, "MESSAGE", None) or str(exc)
            if "TOO_BIG" in str(message).upper() or "ENTITY_TOO_LARGE" in str(message).upper():
                raise UploadError(
                    f"Telegram refused the file as too large for a bot over MTProto "
                    f"({self.capability_mb} MB is the protocol ceiling). Details: {message}"
                ) from exc
            raise UploadError(f"Telegram refused the upload: {message}") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise UploadError(f"The MTProto upload failed: {exc}") from exc

        file_id = ""
        media = getattr(message, "video", None) or getattr(message, "audio", None) or getattr(message, "document", None)
        if media is not None:
            file_id = getattr(media, "file_id", "") or ""

        log.info(
            "MTProto delivered %s to chat %s as message %s (%s MB, %s)",
            path.name,
            chat_id,
            message.id,
            f"{size / MB:.1f}",
            kind,
        )
        return UploadedHandle(
            chat_id=chat_id,
            message_id=message.id,
            size_bytes=size,
            file_id=file_id,
            kind=kind,
            filename=path.name,
        )


def _parse_mode():
    """Pyrogram's HTML parse mode, imported lazily so the module stays cheap to import."""
    from pyrogram.enums import ParseMode

    return ParseMode.HTML


__all__ = ["MtprotoUploader", "UploadedHandle"]
