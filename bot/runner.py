"""Process lifecycle: start the bot, serve the webhook, shut down cleanly.

One code path is used for both modes so behaviour never differs between them:

1. build the application
2. ``initialize()`` → ``start()`` (processing begins)
3. either serve HTTP and register the webhook, or start long polling
4. wait for SIGTERM/SIGINT
5. cancel every job, stop the server, ``stop()`` → ``shutdown()``
"""

from __future__ import annotations

import asyncio
import logging
import signal

from aiohttp import web
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from bot.app import build_application, on_shutdown, on_startup
from bot.runtime import BOT_DATA_KEY, Runtime
from bot.web import create_web_app
from config import Settings

log = logging.getLogger("bot.runner")


async def run_bot(settings: Settings) -> None:
    """Run until the process is asked to stop."""
    application = build_application(settings)
    rt: Runtime = application.bot_data[BOT_DATA_KEY]

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    web_runner: web.AppRunner | None = None

    await application.initialize()
    # initialize() does not call post_init (only run_polling/run_webhook do), so the
    # startup announcement is made explicitly here.
    await on_startup(application)
    await application.start()

    try:
        if settings.runs_webhook:
            web_runner = await _start_web_server(application, settings)
            await _register_webhook(application, settings)
        else:
            await application.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            log.info("Long polling started (no WEBHOOK_URL configured)")

        log.info("Bot is ready — press Ctrl+C to stop")
        await stop.wait()
    finally:
        log.info("Shutting down…")
        await on_shutdown(application)

        for task in list(rt.background_tasks):
            task.cancel()

        if web_runner is not None:
            await web_runner.cleanup()

        try:
            if application.updater is not None and application.updater.running:
                await application.updater.stop()
        except Exception as exc:
            log.debug("Updater stop failed: %s", exc)

        await application.stop()
        await application.shutdown()
        log.info("Stopped. Goodbye.")


async def _start_web_server(application: Application, settings: Settings) -> web.AppRunner:
    web_app = create_web_app(application, settings)
    runner = web.AppRunner(web_app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, settings.listen_host, settings.port)
    await site.start()
    log.info(
        "HTTP server listening on %s:%s — health at /health, webhook at /%s",
        settings.listen_host,
        settings.port,
        settings.webhook_path,
    )
    return runner


async def _register_webhook(application: Application, settings: Settings) -> None:
    try:
        await application.bot.set_webhook(
            url=settings.webhook_target,
            secret_token=settings.webhook_secret or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            max_connections=40,
        )
        log.info("Webhook registered at %s", settings.webhook_target)
    except TelegramError as exc:
        log.error(
            "Could not register the webhook at %s: %s\n"
            "Check that WEBHOOK_URL is the public https address of this service.",
            settings.webhook_target,
            exc,
        )


def _install_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT → graceful shutdown (Render, Voroa and Docker all send SIGTERM)."""
    loop = asyncio.get_running_loop()

    def request_stop(signame: str) -> None:
        if not stop.is_set():
            log.info("Received %s — stopping", signame)
            stop.set()

    for name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_stop, name)
        except (NotImplementedError, RuntimeError):  # pragma: no cover - non-unix
            # Bind the name now: a bare `name` would be late-bound to the last signal.
            signal.signal(sig, lambda *_, _name=name: request_stop(_name))
