"""Application wiring: handlers, timeouts, custom Bot API base URL."""

from __future__ import annotations

import asyncio
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
from bot.access import Access
from bot.assets import AssetLibrary
from bot.handlers import (
    abort_callback,
    about_command,
    cancel_callback,
    cancel_command,
    diag_command,
    help_command,
    id_command,
    limits_command,
    nav_callback,
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
    assets = AssetLibrary(cache_path=settings.resolved_asset_cache_path)
    job_manager = JobManager(settings, application.bot, uploader=uploader, assets=assets)
    application.bot_data[BOT_DATA_KEY] = Runtime(
        settings=settings,
        manager=job_manager,
        tracks=TrackCache(),
        uploader=uploader,
        assets=assets,
        access=Access(settings),
    )

    # Commands -----------------------------------------------------------------
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CommandHandler("limits", limits_command))
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("about", about_command))
    application.add_handler(CommandHandler("diag", diag_command))

    # Links and buttons ---------------------------------------------------------
    application.add_handler(MessageHandler(COMMAND_FILTER | filters.Caption(), on_link))
    application.add_handler(CallbackQueryHandler(quality_callback, pattern=r"^dl:"))
    application.add_handler(CallbackQueryHandler(cancel_callback, pattern=r"^cancel:"))
    application.add_handler(CallbackQueryHandler(abort_callback, pattern=r"^abort:"))
    application.add_handler(CallbackQueryHandler(nav_callback, pattern=r"^nav:"))

    application.add_error_handler(on_error)
    return application


async def on_startup(application: Application) -> None:
    """Called once, after updates are flowing: announce the bot and its commands.

    Nothing here may delay the port or the update loop, and nothing here may take the bot
    down: every step is informational and logs instead of raising.
    """
    rt: Runtime = application.bot_data[BOT_DATA_KEY]
    settings = rt.settings

    await _publish_commands(application)

    # Resolve OWNER_USERNAME → id before anything needs it (a public lookup, once).
    if rt.access is not None and not rt.access.owner_id:
        await rt.access.resolve_owner(application.bot)
    try:
        me = await application.bot.get_me()
        rt.bot_username = me.username or ""
        log.info(
            "Connected as @%s (id %s) — open to everyone; owner is %s",
            me.username,
            me.id,
            rt.access.owner_label if rt.access is not None else "unset",
        )
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


def start_uploader(rt: Runtime, *, attempts: int = 3, delay: float = 20.0) -> None:
    """Warm up the MTProto transport in the background.

    Deliberately *not* awaited by the startup path. Connecting to Telegram over MTProto
    costs seconds to minutes on a cold start (DC discovery, auth-key exchange, DC
    migration), and a web-service platform reports the deploy as failed if the health
    check does not answer meanwhile — so the port is bound first and this runs after it.

    Failures are retried here and are never fatal: the bot keeps serving on the Bot API,
    and :meth:`MtprotoUploader._ensure_client` connects lazily if a large file arrives
    before (or after) the warm-up succeeds.
    """
    uploader: MtprotoUploader | None = rt.uploader
    if uploader is None or not uploader.ready:
        log.info(
            "Large uploads beyond the Bot API's %s MB are unavailable: %s",
            rt.settings.bot_api_capability_mb,
            uploader.reason() if uploader is not None else "no uploader configured",
        )
        return
    rt.track(asyncio.create_task(_connect_uploader(rt, attempts=attempts, delay=delay)))


async def _connect_uploader(rt: Runtime, *, attempts: int, delay: float) -> None:
    """Connect the MTProto client, retrying a few times, and log exactly what happened."""
    uploader: MtprotoUploader = rt.uploader  # type: ignore[assignment]
    for attempt in range(1, attempts + 1):
        try:
            await uploader.start()
            identity = await uploader.identity()
            log.info(
                "Large uploads enabled: MTProto as @%s (id %s), up to %s MB per file",
                identity["username"] or "-",
                identity["id"],
                identity["capability_mb"],
            )
            return
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.warning("MTProto connection attempt %d/%d failed: %s", attempt, attempts, exc)
            if attempt < attempts:
                await asyncio.sleep(delay)
    log.error(
        "MTProto is unreachable after %d attempts — uploads stay on the Bot API (%s MB per file). "
        "The bot is unaffected, and a large-file request will retry the connection.",
        attempts,
        rt.settings.bot_api_capability_mb,
    )


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
