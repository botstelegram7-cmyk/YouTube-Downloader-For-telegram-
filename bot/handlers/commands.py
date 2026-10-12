"""``/`` commands: introduction, help, status, cancel, limits and chat id."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import ContextTypes

from bot import texts
from bot.runtime import runtime
from bot.utils import disk_free_bytes, human_size
from config import PROJECT_NAME, VERSION

log = logging.getLogger("bot.handlers.commands")

HTML = ParseMode.HTML


async def _reply(update: Update, text: str, **kwargs):
    message = update.effective_message
    if message is None:
        return None
    kwargs.setdefault("parse_mode", HTML)
    kwargs.setdefault("disable_web_page_preview", True)
    return await message.reply_text(text, **kwargs)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The introduction: what the bot does, and how to use it."""
    await _reply(update, texts.welcome(PROJECT_NAME, VERSION) + "\n" + texts.commands_list())


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, texts.help_text() + "\n" + texts.commands_list())


async def about_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _reply(update, texts.about_text(VERSION))


async def limits_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime(context)
    settings = rt.settings
    summary = settings.summary()
    # The transport's live state, so the answer never promises more than is reachable
    # *right now* (a client that is still connecting, or has failed, says so).
    summary["mtproto_state"] = getattr(rt.uploader, "state", "off")
    await _reply(update, texts.limits_text(summary))


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    chat = update.effective_chat
    user = update.effective_user
    if chat is None:
        return
    await _reply(
        update,
        "🪪 <b>Identifiers</b>\n"
        f"Chat id: <code>{chat.id}</code>\n"
        f"Your user id: <code>{user.id if user else 'unknown'}</code>\n\n"
        "<i>Set STARTUP_NOTIFY_CHAT_ID to the chat id above to receive the startup summary here.</i>",
    )


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime(context)
    user = update.effective_user
    job = rt.manager.for_user(user.id) if user else None
    active = [job.describe()] if job else []
    stats = rt.manager.stats(rt.settings)
    text = texts.status_text(
        rt.settings.summary(),
        active,
        human_size(disk_free_bytes(rt.settings.download_dir)),
    )
    text += (
        "\n\n<b>This session</b>\n"
        f"• delivered: {stats['completed']}\n"
        f"• failed: {stats['failed']}\n"
        f"• cancelled: {stats['cancelled']}"
    )
    await _reply(update, text)


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime(context)
    user = update.effective_user
    if user is None:
        return
    stopped = await rt.manager.cancel_user(user.id)
    await _reply(update, texts.cancelled() if stopped else texts.nothing_to_cancel())
