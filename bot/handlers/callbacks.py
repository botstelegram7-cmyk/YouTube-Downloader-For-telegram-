"""Inline button presses: pick a quality, cancel a job, dismiss a picker."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import texts
from bot.jobs import Request
from bot.keyboards import diagnostics_keyboard
from bot.qualities import build_options, find_option
from bot.runtime import runtime
from bot.utils import budget_note, disk_free_bytes, html_escape, human_size
from config import VERSION

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
    options = build_options(track, rt.settings, capability_bytes=rt.settings.upload_capability_bytes) if track else []
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
        Request(
            chat_id=chat.id,
            user_id=user.id,
            url=track.url,
            option=option,
            title=track.title,
            token=token,
            track=track,
        ),
        reply_to_message_id=query.message.message_id if query.message else None,
    )


async def torrent_file_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``tf:<index>:<token>`` — download one file out of a torrent."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None or query.message is None:
        return

    parts = (query.data or "").split(":")
    index = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    token = parts[2] if len(parts) > 2 else ""

    rt = runtime(context)
    entry = rt.pending.get(token, user_id=user.id)
    if entry is None or entry.kind != "torrent":
        await query.answer("That torrent has expired — send the link again.", show_alert=False)
        return

    chosen = entry.file_at(index)
    if chosen is None or entry.torrent_path is None or not entry.torrent_path.exists():
        await query.answer("That file is no longer in the list.", show_alert=False)
        return

    await query.answer()
    rt.pending.drop(token)
    _, path, size = chosen
    name = path.rsplit("/", 1)[-1]

    await _edit(
        query,
        f"🧲 <b>{track_name(entry.title)}</b>\n"
        f"File: <b>{name[:90]}</b> ({human_size(size)})\n"
        "<i>Torrent download queued.</i>",
    )

    log.info("User %s picked torrent file #%s (%s) from %r", user.id, index, name[:60], entry.label[:60])
    await rt.manager.start_job(
        Request(
            chat_id=chat.id,
            user_id=user.id,
            url=entry.source.url,
            title=name,
            source=entry.source,
            torrent_path=entry.torrent_path,
            torrent_index=index,
        ),
        reply_to_message_id=query.message.message_id,
    )


async def direct_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``dd:<token>`` — download a plain file URL."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None or query.message is None:
        return

    token = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    rt = runtime(context)
    entry = rt.pending.get(token, user_id=user.id)
    if entry is None or entry.kind != "direct":
        await query.answer("That link has expired — send it again.", show_alert=False)
        return

    await query.answer()
    rt.pending.drop(token)

    await _edit(
        query,
        f"📥 <b>{entry.title[:120]}</b>\n<i>Direct download queued.</i>",
    )

    log.info("User %s started a direct download from %s", user.id, entry.label[:60])
    await rt.manager.start_job(
        Request(
            chat_id=chat.id,
            user_id=user.id,
            url=entry.source.url,
            title=entry.title,
            source=entry.source,
        ),
        reply_to_message_id=query.message.message_id,
    )


def track_name(value: str) -> str:
    """Escape a torrent's name for HTML, trimmed to something a phone screen can show."""
    return html_escape((value or "")[:120])


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


async def retry_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``retry:<token>`` — re-read the link and offer the picker again."""
    query = update.callback_query
    user = update.effective_user
    chat = update.effective_chat
    if query is None or user is None or chat is None or query.message is None:
        return

    token = (query.data or "").split(":", 1)[1] if ":" in (query.data or "") else ""
    rt = runtime(context)
    source = rt.manager.sources.get(token, user_id=user.id)
    if source is None:
        await query.answer("That link has expired — send it again.", show_alert=True)
        return

    await query.answer("Reading the link again…")
    from bot.handlers.links import reoffer

    problem = await reoffer(
        rt=rt, chat_id=chat.id, user_id=user.id, source=source, status_message=query.message
    )
    if problem:
        await _edit(query, texts.link_refused(problem))


async def _edit(query, text: str, *, markup=None) -> None:
    try:
        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=markup,
        )
    except Exception as exc:
        log.debug("Could not edit the callback message: %s", exc)

async def nav_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The ``nav:`` buttons on the /start screen (and the /diag refresh)."""
    query = update.callback_query
    if query is None or query.data is None:
        return
    await query.answer()
    rt = runtime(context)
    action = query.data.split(":", 1)[1]
    user = update.effective_user

    if action == "diag":
        if not rt.access.is_owner(user.id if user else None):
            await _edit(query, texts.owner_only("/diag"))
            return
    elif action == "help":
        owner = rt.access.is_owner(user.id if user else None)
        await _edit(query, texts.help_text() + "\n" + texts.commands_list(include_owner=owner))
        return
    elif action == "status":
        stats = rt.manager.stats(rt.settings)
        text = texts.status_text(
            rt.settings.summary(),
            [],
            human_size(disk_free_bytes(rt.settings.download_dir)),
        )
        text += (
            "\n\n<b>This session</b>\n"
            f"• delivered: {stats['completed']}\n"
            f"• failed: {stats['failed']}\n"
            f"• cancelled: {stats['cancelled']}"
        )
        await _edit(query, text)
        return
    elif action == "limits":
        summary = rt.settings.summary()
        summary["mtproto_state"] = getattr(rt.uploader, "state", "off")
        await _edit(query, texts.limits_text(summary))
        return
    elif action == "about":
        await _edit(query, texts.about_text(VERSION))
        return

    # Everything else, and the diag refresh: rebuild the diagnostics panel.
    stats = rt.manager.stats(rt.settings)
    facts = {
        "cpu": budget_note(),
        "mtproto": "unavailable" if rt.uploader is None else getattr(rt.uploader, "state", "off"),
        "mtproto_error": "" if rt.uploader is None else getattr(rt.uploader, "last_error", ""),
        "cached_tracks": len(rt.tracks),
        "assets": rt.assets.describe() if rt.assets is not None else {},
        "notes": rt.settings.warnings(),
    }
    await _edit(query, texts.diagnostics(rt.settings.summary(), stats, facts), markup=diagnostics_keyboard())
