"""Incoming messages: turn a link into a quality picker.

Inspection happens in a background task so the update is acknowledged instantly —
important in webhook mode, where Telegram retries when a handler is slow.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from bot import texts
from bot.errors import BotError
from bot.keyboards import quality_keyboard
from bot.qualities import build_options, default_option
from bot.runtime import Runtime, runtime
from bot.typetube import Track, resolve
from bot.urls import youtube_link
from bot.utils import human_duration
from config import Settings

log = logging.getLogger("bot.handlers.links")

#: Inspector timeout: faster than Telegram's own webhook timeout.
INSPECT_TIMEOUT = 45.0


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for every text/caption message."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    text = (message.text or message.caption or "").strip()
    url, refusal = youtube_link(text)

    if url is None:
        # Only nudge in private chats, and only for something that looks like an
        # attempt at a link — never answer ordinary conversation in groups.
        if chat.type == ChatType.PRIVATE and text and not text.startswith("/"):
            if refusal:
                log.info("Refused a link from user %s: %s", user.id, refusal)
                await message.reply_text(texts.not_youtube(refusal), parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(texts.not_supported(text), parse_mode=ParseMode.HTML)
        return

    rt = runtime(context)
    status = await message.reply_text(texts.analyzing(url), parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    task = asyncio.create_task(
        _inspect_and_offer(
            rt=rt,
            settings=rt.settings,
            chat_id=chat.id,
            user_id=user.id,
            url=url,
            status=status,
            reply_to=message.message_id,
        )
    )
    rt.track(task)


async def _inspect_and_offer(
    *,
    rt: Runtime,
    settings: Settings,
    chat_id: int,
    user_id: int,
    url: str,
    status,
    reply_to: int | None,
) -> None:
    """Ask TypeTube what the video offers and show the qualities it really has."""
    try:
        track: Track = await asyncio.wait_for(resolve(url, settings), timeout=INSPECT_TIMEOUT)
    except asyncio.TimeoutError:
        await _safe_edit(status, texts.resolve_failed(url, "Reading the video took too long."))
        return
    except BotError as exc:
        await _safe_edit(status, texts.resolve_failed(url, exc.reason))
        return
    except Exception as exc:
        log.exception("Unexpected resolve failure for %s", url[:80])
        await _safe_edit(status, texts.resolve_failed(url, str(exc)[:200]))
        return

    limit_minutes = settings.max_duration_minutes
    if limit_minutes and track.duration > limit_minutes * 60:
        await _safe_edit(status, texts.too_long(track.duration, limit_minutes))
        return

    options = build_options(track, settings, capability_bytes=settings.upload_capability_bytes)
    if not options:
        await _safe_edit(status, texts.no_streams())
        return

    token = rt.tracks.put(track, user_id=user_id)
    recommended = default_option(options, settings)

    body = texts.choose_quality(
        track.title,
        human_duration(track.duration),
        track.author,
        settings.upload_capability_mb,
        replaces_running=rt.manager.for_user(user_id) is not None,
    )

    try:
        await status.edit_text(
            body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=quality_keyboard(options, token, recommended=recommended),
        )
    except Exception as exc:
        log.debug("Could not show the quality keyboard: %s", exc)


async def _safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as exc:
        log.debug("Could not edit the status message: %s", exc)
