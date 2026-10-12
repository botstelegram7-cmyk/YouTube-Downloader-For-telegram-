"""Inline button presses: pick a quality, cancel a job, dismiss a picker."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import texts
from bot.qualities import build_options, find_option
from bot.runtime import runtime

log = logging.getLogger("bot.handlers.callbacks")


async def quality_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``dl:<quality>:<token>`` — start the download the user asked for."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None:
        return

    await query.answer()

    parts = (query.data or "").split(":")
    key = parts[1] if len(parts) > 1 else ""
    token = parts[2] if len(parts) > 2 else ""

    rt = runtime(context)
    track = rt.tracks.get(token, user_id=user.id)
    options = build_options(track, rt.settings, upload_limit_bytes=rt.settings.upload_limit_bytes) if track else []
    option = find_option(options, key) if track else None

    if track is None or option is None:
        await _edit(query, "⌛️ That choice has expired — send the link again and I will read it afresh.")
        return

    rt.tracks.drop(token)

    await _edit(
        query,
        f"🎯 <b>{track.title[:90]}</b>\n"
        f"Quality: <b>{option.label}</b>\n"
        f"<i>{'Audio download' if option.is_audio else 'Video download'} queued.</i>",
    )

    log.info("User %s chose %s for %s", user.id, option.key, track.url[:80])
    await rt.manager.start_job(
        chat_id=chat.id,
        user_id=user.id,
        url=track.url,
        option=option,
        title=track.title,
        token=token,
        track=track,
        reply_to_message_id=query.message.message_id if query.message else None,
    )


async def cancel_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``cancel:<job id>`` — the button on the live status message."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return

    job_id = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    rt = runtime(context)
    job = rt.manager.for_user(user.id)

    if job is None or (job_id and job.id != job_id):
        await query.answer("That download is already finished.", show_alert=False)
        return

    await query.answer("Cancelling…")
    await rt.manager.cancel_user(user.id)


async def abort_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``abort:<token>`` — dismiss the quality picker without downloading."""
    query = update.callback_query
    user = update.effective_user
    if query is None or user is None:
        return
    await query.answer()
    token = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    rt = runtime(context)
    rt.tracks.drop(token)
    await _edit(query, texts.picker_dismissed())


async def _edit(query, text: str) -> None:
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=None)
    except Exception as exc:
        log.debug("Could not edit the callback message: %s", exc)
