"""Process lifecycle: start the bot, serve the webhook, shut down cleanly.

One code path is used for both modes so behaviour never differs between them:

1. build the application and **bind the port** — a web-service platform kills any deploy
   whose health check does not answer in time, so this happens before any network call
2. ``initialize()`` → ``start()`` (processing begins)
3. either register the webhook, or start long polling
4. announce the startup (commands, introduction) and then connect the large-file
   transport — both after updates already flow, and never in the critical path
5. wait for SIGTERM/SIGINT
6. cancel every job, stop the server, ``stop()`` → ``shutdown()``

Step 1 is load-bearing. Telegram's MTProto handshake can take minutes on a cold start
(DC discovery, auth-key exchange, DC migration), and an earlier version of this file
awaited it before binding the port: the deploy then failed its health check while the
client was still negotiating, even though the bot itself was perfectly healthy.
"""

from __future__ import annotations

import asyncio
import logging
import signal
import time

from aiohttp import web
from telegram import Update
from telegram.error import TelegramError
from telegram.ext import Application

from bot.app import build_application, on_shutdown, on_startup, start_uploader
from bot.runtime import BOT_DATA_KEY, Runtime
from bot.web import create_web_app
from config import Settings

log = logging.getLogger("bot.runner")


async def run_bot(settings: Settings) -> None:
    """Run until the process is asked to stop."""
    boot = time.monotonic()

    def phase(label: str) -> None:
        log.info("startup[%.2fs] %s", time.monotonic() - boot, label)

    application = build_application(settings)
    rt: Runtime = application.bot_data[BOT_DATA_KEY]

    stop = asyncio.Event()
    _install_signal_handlers(stop)

    web_runner: web.AppRunner | None = None

    try:
        # 1. Answer the platform's health check first, in both modes: /health and /stats
        #    are cheap and work before Telegram is contacted.
        web_runner = await _start_web_server(application, settings)
        phase(f"HTTP server listening on {settings.listen_host}:{settings.port}")

        # 2. Wire the Telegram side.
        await application.initialize()
        phase("Telegram application initialised")
        await application.start()
        phase("handlers live")

        if settings.runs_webhook:
            if await _register_webhook(application, settings):
                phase("webhook registered")
            else:
                # Telegram will not deliver a single update to a webhook it refused, and a
                # bot that receives no updates looks "offline" to its users. Polling needs
                # no inbound route, so it keeps the bot usable while the operator fixes the
                # webhook. An earlier version logged "webhook registered" no matter what.
                await _start_polling(application)
                phase("webhook REFUSED by Telegram — long polling started instead")
        else:
            await _start_polling(application)
            phase("long polling started (no WEBHOOK_URL configured)")

        # 3. Announce ourselves. initialize() does not call post_init (only
        #    run_polling/run_webhook do), so the startup announcement is made explicitly —
        #    and only now, because it is informational and must not delay the port.
        await on_startup(application)
        phase("startup announcement sent")

        # 4. Finally, the large-file transport. Connecting to Telegram over MTProto can
        #    take a long time (auth key, DC migration) and is retried in the background:
        #    it decides the *upload ceiling*, never whether the service comes up.
        start_uploader(rt)
        phase("MTProto warm-up scheduled")

        log.info("Bot is ready — press Ctrl+C to stop")
        await stop.wait()
    finally:
        log.info("Shutting down…")

        # Stop the fire-and-forget work (polling the uploader connection) before the
        # uploader itself is closed, so nothing reconnects during shutdown.
        pending = [task for task in list(rt.background_tasks) if not task.done()]
        for task in pending:
            task.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

        await on_shutdown(application)

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
        "HTTP server listening on %s:%s — health at /health%s",
        settings.listen_host,
        settings.port,
        f", webhook at /{settings.webhook_path}" if settings.runs_webhook else " (polling mode: /health only)",
    )
    return runner


async def _start_polling(application: Application) -> None:
    """Fetch updates by long polling — the mode that needs no inbound route."""
    await application.updater.start_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=True,
    )


async def _register_webhook(application: Application, settings: Settings) -> bool:
    """Point Telegram at this service. Returns True only if Telegram accepted the URL.

    The caller must not report success on a failure: a refused webhook means zero incoming
    updates, which is exactly what "the bot went offline" looks like from the outside.
    """
    try:
        await application.bot.set_webhook(
            url=settings.webhook_target,
            secret_token=settings.webhook_secret or None,
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
            max_connections=40,
        )
    except TelegramError as exc:
        hint = ""
        if "secret token" in str(exc).lower():
            hint = " WEBHOOK_SECRET must use only A-Z a-z 0-9 _ - (it is cleaned at startup)."
        log.error(
            "Telegram refused the webhook at %s: %s\n"
            "Check that the URL is the public https address of this service and that it "
            "resolves from the internet.%s",
            settings.webhook_target,
            exc,
            hint,
        )
        return False
    log.info("Webhook registered at %s", settings.webhook_target)
    return True


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
