"""Delivering large files through a Telegram *user session* (MTProto, via Pyrofork).

The Bot API caps a bot's uploads at 50 MB, and a self-hosted Bot API server needs another
service to run. A user session sidesteps both: MTProto user accounts may upload **2000 MB**
per file (4000 MB with Telegram Premium), so the bot uploads through the session and then
hands the file over with the Bot API — which copies it *server-side*, so no bytes cross the
network twice and no "Forwarded from" header appears:

    1. session.send_video(...)      → uploads the file into the bot's own chat
    2. bot.copy_message(...)        → delivers it to the user, as the bot

Only credentials are needed: ``TELEGRAM_API_ID``, ``TELEGRAM_API_HASH`` and a session
string from ``scripts/make_session.py``. Nothing extra has to be hosted.

Everything here degrades politely: without credentials the uploader reports why it is not
ready, and the caller falls back to the Bot API paths in :mod:`bot.upload`.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from pathlib import Path

from bot.download import ProgressUpdate
from bot.errors import UploadCancelled, UploadError
from config import VERSION, Settings

log = logging.getLogger("bot.mtproto")


class _Aborted(Exception):
    """Raised inside Pyrofork's progress callback to stop an upload at once."""


@dataclass
class UploadedHandle:
    """Where the user session left the uploaded file."""

    chat_id: int
    message_id: int
    size_bytes: int
    file_id: str
    account_id: int
    kind: str  # "video" | "audio"
    filename: str = ""


class MtprotoUploader:
    """A lazily started Pyrofork client that uploads for the bot.

    One client is shared by every job: connecting is expensive and Telegram counts
    sessions, so it is started once at boot and stopped at shutdown.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._client = None
        self._lock = asyncio.Lock()
        self._account: dict | None = None

    # -- availability -------------------------------------------------------
    def reason(self) -> str:
        """Why the session cannot be used, or ``""`` when it is ready."""
        missing = []
        if not self.settings.api_id:
            missing.append("TELEGRAM_API_ID")
        if not self.settings.api_hash:
            missing.append("TELEGRAM_API_HASH")
        if not self.settings.session_string:
            missing.append("TELEGRAM_SESSION_STRING")
        if missing:
            return (
                "MTProto upload is not configured (missing "
                + ", ".join(missing)
                + "). Get the first two from https://my.telegram.org and generate the session "
                "string with `python scripts/make_session.py`."
            )
        if not self.settings.bot_id:
            return "BOT_TOKEN does not contain a usable bot id, so there is nowhere to upload to."
        return ""

    @property
    def ready(self) -> bool:
        return not self.reason()

    @property
    def upload_cap_mb(self) -> int:
        return self.settings.mtproto_cap_mb

    # -- lifecycle ----------------------------------------------------------
    async def start(self) -> bool:
        """Connect the session. Returns True when it is usable."""
        if not self.ready:
            log.info("MTProto upload disabled: %s", self.reason())
            return False

        from pyrogram import Client

        async with self._lock:
            if self._client is not None:
                return True
            client = Client(
                name=":memory:",
                api_id=self.settings.api_id,
                api_hash=self.settings.api_hash,
                session_string=self.settings.session_string,
                in_memory=True,
                no_updates=True,  # nothing is read from this session
                max_concurrent_transmissions=self.settings.mt_connections,
                device_model="YouTube Downloader Bot",
                app_version=VERSION,
            )
            try:
                await client.start()
            except Exception as exc:
                log.error("MTProto session could not start: %s", exc)
                raise UploadError(
                    "The Telegram session could not connect — check TELEGRAM_API_ID, TELEGRAM_API_HASH and "
                    f"TELEGRAM_SESSION_STRING (generate it again with scripts/make_session.py). Details: {exc}"
                ) from exc
            self._client = client
            log.info("MTProto session connected (limit %s MB per file)", self.settings.mtproto_cap_mb)
            return True

    async def stop(self) -> None:
        client, self._client = self._client, None
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
            raise UploadError(self.reason() or "The MTProto session is not available.")
        return self._client

    # -- account ------------------------------------------------------------
    async def account(self) -> dict:
        """The signed-in account: id, name, and whether it is Premium."""
        if self._account is not None:
            return self._account
        client = await self._ensure_client()
        me = await client.get_me()
        premium = bool(getattr(me, "is_premium", False))
        self._account = {
            "id": me.id,
            "name": " ".join(part for part in (me.first_name, me.last_name) if part) or (me.username or ""),
            "username": me.username or "",
            "premium": premium,
            "phone": getattr(me, "phone_number", "") or "",
            "cap_mb": int((getattr(me, "is_premium", False) and 4000) or self.settings.mtproto_cap_mb),
        }
        log.info(
            "MTProto account: %s (@%s, id %s, premium=%s → up to %s MB per file)",
            self._account["name"],
            self._account["username"] or "-",
            self._account["id"],
            premium,
            self._account["cap_mb"],
        )
        return self._account

    # -- uploading ----------------------------------------------------------
    def _progress(self, on_progress, cancel_event, filename: str):
        """Bridge Pyrofork's progress callback to the status message, with cancellation."""

        def callback(current: int, total: int, *args) -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise _Aborted()
            if on_progress is None:
                return
            now = time.monotonic()
            elapsed = max(0.001, now - started["at"])
            speed = current / elapsed
            percent = min(100.0, current * 100.0 / total) if total else None
            eta = (total - current) / speed if (total and speed > 0) else None
            on_progress(
                ProgressUpdate(
                    phase="upload",
                    percent=percent,
                    downloaded=current,
                    total=total or None,
                    speed=speed,
                    eta=eta,
                    note=f"Uploading {filename}…",
                )
            )

        started = {"at": time.monotonic()}
        return callback

    async def upload(
        self,
        path: Path,
        *,
        kind: str,
        title: str = "",
        artist: str = "",
        duration: float = 0.0,
        width: int = 0,
        height: int = 0,
        thumbnail: Path | None = None,
        on_progress=None,
        cancel_event: asyncio.Event | None = None,
    ) -> UploadedHandle:
        """Upload one file with the user session and return where it landed.

        The destination is the bot's own chat (``STORAGE_CHAT_ID`` can override that), which
        is what makes the follow-up ``copy_message`` possible.
        """
        if not self.ready:
            raise UploadError(self.reason())

        client = await self._ensure_client()
        target = self.settings.storage_chat_id or self.settings.bot_id
        thumb = str(thumbnail) if thumbnail is not None and Path(thumbnail).is_file() else None
        progress = self._progress(on_progress, cancel_event, path.name)
        size = path.stat().st_size

        from pyrogram.errors import FloodWait, RPCError

        try:
            if kind == "video":
                message = await client.send_video(
                    chat_id=target,
                    video=str(path),
                    thumb=thumb,
                    duration=int(duration) or None,
                    width=width or None,
                    height=height or None,
                    file_name=path.name,
                    supports_streaming=True,  # so Telegram can stream it while downloading
                    disable_notification=True,
                    progress=progress,
                )
                file_id = getattr(message.video, "file_id", "") if message.video else ""
            else:
                message = await client.send_audio(
                    chat_id=target,
                    audio=str(path),
                    thumb=thumb,
                    duration=int(duration) or None,
                    performer=(artist or "")[:64] or None,
                    title=(title or path.stem)[:64],
                    file_name=path.name,
                    disable_notification=True,
                    progress=progress,
                )
                file_id = getattr(message.audio, "file_id", "") if message.audio else ""
        except _Aborted as exc:
            raise UploadCancelled() from exc
        except FloodWait as exc:
            raise UploadError(f"Telegram asked me to wait {exc.value}s before uploading again.") from exc
        except RPCError as exc:
            raise UploadError(f"Telegram refused the upload: {getattr(exc, 'MESSAGE', str(exc))}") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise UploadError(f"The MTProto upload failed: {exc}") from exc

        account = await self.account()
        log.info(
            "MTProto uploaded %s (%s) as %s in chat %s (%s MB)",
            path.name,
            kind,
            message.id,
            target,
            f"{size / 1e6:.1f}",
        )
        return UploadedHandle(
            chat_id=target,
            message_id=message.id,
            size_bytes=size,
            file_id=file_id,
            account_id=account["id"],
            kind=kind,
            filename=path.name,
        )

    async def delete(self, handle: UploadedHandle) -> None:
        """Remove the uploaded copy once it has been delivered (best effort)."""
        client = self._client
        if client is None:
            return
        try:
            await client.delete_messages(chat_id=handle.chat_id, message_ids=handle.message_id)
        except Exception as exc:
            log.debug("Could not delete the storage message %s: %s", handle.message_id, exc)


__all__ = ["MtprotoUploader", "UploadedHandle"]
