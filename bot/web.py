"""The web-service surface: health check, status endpoint and the webhook.

A Web Service (Render, Voroa, Fly, a VPS…) needs a port that answers. This module
provides exactly that:

* ``GET /``       — service card (name, version, mode)
* ``GET /health`` — what the platform's health check should point at
* ``GET /stats``  — queue and job counters
* ``POST /<path>``— the Telegram webhook (path from ``WEBHOOK_PATH``)
"""

from __future__ import annotations

import json
import logging
import time

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from bot.ffmpeg import ffmpeg_version
from bot.runtime import BOT_DATA_KEY, Runtime
from bot.utils import disk_free_bytes, human_size
from config import PROJECT_NAME, VERSION, Settings

log = logging.getLogger("bot.web")

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


def create_web_app(application: Application, settings: Settings) -> web.Application:
    """Build the aiohttp application served by the web-service deployment."""
    runtime: Runtime = application.bot_data[BOT_DATA_KEY]
    started = time.monotonic()

    async def root(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": PROJECT_NAME,
                "version": VERSION,
                "mode": "webhook" if settings.runs_webhook else "polling",
                "api_mode": settings.api_mode,
                "upload_limit_mb": settings.upload_limit_mb,
            }
        )

    async def health(_request: web.Request) -> web.Response:
        ffmpeg = await ffmpeg_version(settings)
        payload = {
            "ok": True,
            "service": PROJECT_NAME,
            "version": VERSION,
            "uptime_seconds": round(time.monotonic() - started),
            "mode": "webhook" if settings.runs_webhook else "polling",
            "api_mode": settings.api_mode,
            "upload_limit_mb": settings.upload_limit_mb,
            "api_upload_cap_mb": settings.api_upload_cap_mb,
            "jobs": runtime.manager.stats(settings),
            "runtime": {
                "ffmpeg": ffmpeg or "missing",
                "free_disk": human_size(disk_free_bytes(settings.download_dir)),
                "concurrent_jobs": settings.max_concurrent_jobs,
            },
        }
        status = 200 if ffmpeg else 503
        payload["ok"] = bool(ffmpeg)
        if not ffmpeg:
            payload["error"] = "FFmpeg is not available"
        return web.json_response(payload, status=status)

    async def stats(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "jobs": runtime.manager.snapshot(),
                "counters": runtime.manager.stats(settings),
                "cached_tracks": len(runtime.tracks),
            }
        )

    async def webhook(request: web.Request) -> web.Response:
        if settings.webhook_secret:
            provided = request.headers.get(SECRET_HEADER, "")
            if provided != settings.webhook_secret:
                log.warning("Rejected a webhook call with a bad secret token")
                return web.json_response({"ok": False, "error": "forbidden"}, status=403)

        if request.content_length and request.content_length > MAX_PAYLOAD_BYTES:
            return web.json_response({"ok": False, "error": "payload too large"}, status=413)

        try:
            payload = await request.json()
        except (json.JSONDecodeError, ValueError):
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        try:
            update = Update.de_json(payload, application.bot)
        except Exception as exc:
            log.warning("Could not decode an update: %s", exc)
            return web.json_response({"ok": True, "ignored": True})

        if update is None:
            return web.json_response({"ok": True, "ignored": True})

        # Hand the update to python-telegram-bot and answer immediately, so a long
        # job never makes Telegram retry the delivery.
        await application.update_queue.put(update)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/stats", stats)
    app.router.add_post(f"/{settings.webhook_path.strip('/')}", webhook)
    return app
