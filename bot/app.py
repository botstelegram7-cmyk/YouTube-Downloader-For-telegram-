"""Application wiring: handlers, timeouts, custom Bot API base URL."""

from __future__ import annotations

import logging

from telegram import BotCommand, BotCommandScopeDefault
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    AIORateLimiter,
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    filters,
)

from bot import texts
from bot.handlers import (
    abort_callback,
    about_command,
    cancel_callback,
    cancel_command,
    help_command,
    id_command,
    limits_command,
    on_error,
    on_link,
    quality_callback,
    start_command,
    status_command,
)
from bot.jobs import JobManager
from bot.mtproto import MtprotoUploader
from bot.runtime import BOT_DATA_KEY, Runtime
from bot.typetube import TrackCache
from config import VERSION, Settings

log = logging.getLogger("bot.app")

#: Command names Telegram accepts.
COMMAND_FILTER = filters.TEXT & ~filters.COMMAND


def build_application(settings: Settings) -> Application:
    """Create the ``Application`` with handlers, limits and base URLs."""
    builder = (
        ApplicationBuilder()
        .token(settings.bot_token)
        .concurrent_updates(True)
        .connect_timeout(30.0)
        .read_timeout(60.0)
        .write_timeout(600.0)
        .pool_timeout(30.0)
        .get_updates_connect_timeout(30.0)
        .get_updates_read_timeout(30.0)
    )

    if settings.uses_local_api:
        # A self-hosted Bot API server: same paths as Telegram's, different host.
        base = settings.api_base
        builder = builder.base_url(f"{base}/bot").base_file_url(f"{base}/file/bot")
        log.info("Using a local Bot API server at %s", base)

    try:
        builder = builder.rate_limiter(AIORateLimiter())
    except Exception as exc:
        log.warning("Rate limiter unavailable (%s); install python-telegram-bot[rate-limiter]", exc)

    application = builder.build()

    uploader = MtprotoUploader(settings)
    job_manager = JobManager(settings, application.bot, uploader=uploader)
    application.bot_data[BOT_DATA_KEY] = Runtime(
        settings=settings,
        manager=job_manager,
        tracks=TrackCache(),
        uploader=uploader,
    )

    # Commands -----------------------------------------------------------------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("limits", limits_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("about", about_command))

    # Links and buttons ---------------------------------------------------------
    application.add_handler(MessageHandler(COMMAND_FILTER | filters.Caption(), on_link))
    application.add_handler(CallbackQueryHandler(quality_callback, pattern=r"^dl:"))
    application.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel:"))
    application.add_handler(CallbackQueryHandler(abort_callback, pattern=r"^abort:"))

    application.add_error_handler(on_error)
    return application


async def on_startup(application: Application) -> None:
    """Called once, after ``initialize()``: announce the bot and its commands."""
    rt: Runtime = application.bot_data[BOT_DATA_KEY]
    settings = rt.settings

    await _publish_commands(application)

    uploader: MtprotoUploader | None = rt.uploader
    if uploader is not None and uploader.ready:
        try:
            await uploader.start()
            identity = await uploader.identity()
            log.info(
                "Large uploads enabled: MTProto as @%s (id %s), up to %s MB per file",
                identity["username"] or "-",
                identity["id"],
                identity["capability_mb"],
            )
        except Exception as exc:
            log.error("MTProto session unavailable, large uploads disabled: %s", exc)
    elif uploader is not None:
        log.info(
            "Large uploads beyond the Bot API's %s MB are unavailable: %s",
            settings.bot_api_capability_mb,
            uploader.reason(),
        )

    try:
        me = await application.bot.get_me()
        log.info("Connected as @%s (id %s)", me.username, me.id)
    except TelegramError as exc:
        log.error("Could not verify the bot token with getMe: %s", exc)
        return

    target = settings.startup_notify_chat_id or settings.owner_user_id
    if not target:
        log.info("Set STARTUP_NOTIFY_CHAT_ID (see /id) to receive the startup summary in Telegram")
        return

    report = texts.startup_report(VERSION, settings.summary(), settings.warnings())
    try:
        await application.bot.send_message(
            chat_id=target,
            text=report,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
        )
        log.info("Startup summary delivered to chat %s", target)
    except TelegramError as exc:
        log.warning("Could not deliver the startup summary to %s: %s", target, exc)


async def _publish_commands(application: Application) -> None:
    """Register the command menu users see in the Telegram UI."""
    commands = [BotCommand(command=name, description=text) for name, text in texts.COMMANDS]
    try:
        await application.bot.set_my_commands(commands, scope=BotCommandScopeDefault())
    except TelegramError as exc:
        log.warning("Could not publish the command menu: %s", exc)


async def on_shutdown(application: Application) -> None:
    """Stop every job and free its disk before the process exits."""
    rt: Runtime | None = application.bot_data.get(BOT_DATA_KEY)
    if rt is None:
        return
    stopped = await rt.manager.cancel_all()
    if stopped:
        log.info("Cancelled %s job(s) during shutdown", stopped)

    uploader = getattr(rt, "uploader", None)
    if uploader is not None:
        await uploader.stop()
