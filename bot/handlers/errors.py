"""Global error handler: log everything, tell the user something useful."""

from __future__ import annotations

import logging

from telegram import Update
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, RetryAfter
from telegram.ext import ContextTypes

from bot import texts

log = logging.getLogger("bot.handlers.errors")


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Last line of defence — never raise out of here."""
    error = context.error
    log.exception("Unhandled exception while processing an update", exc_info=error)

    if isinstance(error, RetryAfter):
        log.warning("Telegram rate limit: retry after %ss", error.retry_after)
        return
    if isinstance(error, Forbidden):
        log.warning("The bot was blocked or removed by the user")
        return

    if not isinstance(update, Update):
        return

    message = update.effective_message
    if message is None:
        return

    text = texts.job_failed("Something went wrong while handling that. Please try again.")
    try:
        await message.reply_text(text, parse_mode=ParseMode.HTML)
    except BadRequest as exc:
        log.debug("Could not deliver the error notice: %s", exc)
