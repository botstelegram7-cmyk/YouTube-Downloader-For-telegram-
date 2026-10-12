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
from bot.probe import MediaInfo, probe
from bot.qualities import sort_options
from bot.runtime import Runtime, runtime
from bot.urls import check_url_safety_async, extract_url
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
    url = extract_url(text)

    if url is None:
        # Only nudge in private chats, and only for something that looks like an
        # attempt at a link — never answer ordinary conversation in groups.
        if chat.type == ChatType.PRIVATE and text and not text.startswith("/"):
            await message.reply_text(texts.not_supported(text), parse_mode=ParseMode.HTML)
        return

    allowed, reason = await check_url_safety_async(url, allow_private=runtime(context).settings.allow_private_urls)
    if not allowed:
        log.warning("Blocked a link from user %s: %s (%s)", user.id, url[:80], reason)
        await message.reply_text(texts.blocked_url(reason), parse_mode=ParseMode.HTML)
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
    """Read the link's formats and offer the qualities it really has."""
    try:
        info: MediaInfo = await asyncio.wait_for(probe(url, settings), timeout=INSPECT_TIMEOUT)
    except asyncio.TimeoutError:
        await _safe_edit(status, texts.probe_failed(url, "Reading the link took too long."))
        return
    except BotError as exc:
        await _safe_edit(status, texts.probe_failed(url, exc.reason))
        return
    except Exception as exc:
        log.exception("Unexpected probe failure for %s", url[:80])
        await _safe_edit(status, texts.probe_failed(url, str(exc)[:200]))
        return

    token = rt.probes.put(info, user_id=user_id)
    options = sort_options(info.options)

    body = texts.choose_quality(
        info.title,
        human_duration(info.duration),
        settings.upload_limit_mb,
    )
    if rt.manager.for_user(user_id) is not None:
        body = "♻️ <i>Your current download will be cancelled when you pick one.</i>\n\n" + body

    if info.uploader:
        body = f"📺 {info.uploader[:60]}\n" + body

    try:
        await status.edit_text(
            body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=quality_keyboard(options, token),
        )
    except Exception as exc:
        log.debug("Could not show the quality keyboard: %s", exc)


async def _safe_edit(message, text: str) -> None:
    try:
        await message.edit_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True)
    except Exception as exc:
        log.debug("Could not edit the status message: %s", exc)
