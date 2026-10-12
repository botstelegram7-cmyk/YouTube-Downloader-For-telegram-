"""``/`` commands: introduction, help, status, cancel, limits, diagnostics and chat id."""

from __future__ import annotations

import logging

from telegram import InputFile, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import ContextTypes

from bot import keyboards, texts
from bot.runtime import runtime
from bot.utils import budget_note, disk_free_bytes, human_size
from config import PROJECT_NAME, VERSION

log = logging.getLogger("bot.handlers.commands")

HTML = ParseMode.HTML

# Telegram caps photo and animation captions at 1024 characters; text messages get 4096.
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096


async def _reply(update: Update, text: str, **kwargs):
    message = update.effective_message
    if message is None:
        return None
    kwargs.setdefault("parse_mode", HTML)
    kwargs.setdefault("disable_web_page_preview", True)
    return await message.reply_text(text, **kwargs)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """The introduction: a picture, what the bot takes, and a couple of shortcuts.

    No command cheat sheet, no engine name, no size ceilings — that is what ``/help`` and
    ``/limits`` are for. The operator's credit is opt-in (``SHOW_OWNER_IN_START``).
    """
    rt = runtime(context)
    message = update.effective_message
    if message is None:
        return

    show_owner = rt.settings.show_owner_in_start
    text = texts.welcome(
        PROJECT_NAME, VERSION, owner=rt.owner_label, show_owner=show_owner
    )
    markup = keyboards.welcome_keyboard(
        bot_username=rt.bot_username,
        owner_username=rt.settings.owner_username,
        show_owner=show_owner,
    )

    asset = rt.assets.get("start.jpg") if rt.assets is not None else None
    if asset is not None and rt.settings.rich_messages:
        photo = rt.assets.file_id(asset.name) or InputFile(asset.bytes(), filename=asset.name)
        try:
            sent = await message.reply_photo(
                photo=photo,
                caption=text[:CAPTION_LIMIT],
                parse_mode=HTML,
                reply_markup=markup,
            )
            # Telegram returns an id for the bytes it stored; reuse it forever after.
            photos = getattr(sent, "photo", None) or []
            if photos:
                rt.assets.remember(asset.name, photos[-1].file_id)
            return
        except TelegramError as exc:
            log.warning("Could not send the start picture (%s); falling back to text", exc)

    await message.reply_text(
        text[:MESSAGE_LIMIT],
        parse_mode=HTML,
        disable_web_page_preview=True,
        reply_markup=markup,
    )


async def diag_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """``/diag`` — owner-only view of the host, the queue and the transports."""
    rt = runtime(context)
    user = update.effective_user
    if not rt.access.is_owner(user.id if user else None):
        await _reply(update, texts.owner_only("/diag"))
        return

    stats = rt.manager.stats(rt.settings)
    runtime_facts = {
        "cpu": budget_note(),
        "mtproto": "unavailable" if rt.uploader is None else getattr(rt.uploader, "state", "off"),
        "mtproto_error": "" if rt.uploader is None else getattr(rt.uploader, "last_error", ""),
        "cached_tracks": len(rt.tracks),
        "assets": rt.assets.describe() if rt.assets is not None else {},
        "notes": rt.settings.warnings(),
    }
    await _reply(update, texts.diagnostics(rt.settings.summary(), stats, runtime_facts))


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rt = runtime(context)
    user = update.effective_user
    owner = rt.access.is_owner(user.id if user else None)
    await _reply(update, texts.help_text() + "\n" + texts.commands_list(include_owner=owner))


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
