"""The web-service surface: health check, status endpoint and the webhook.

A Web Service (Render, Voroa, Fly, a VPS…) needs a port that answers. This module
provides exactly that:

* ``GET /``       — service card (name, version, mode)
* ``GET /health`` — what the platform's health check should point at
* ``GET /stats``  — queue and job counters
* ``POST /<path>``— the Telegram webhook (path from ``WEBHOOK_PATH``)
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from bot.ffmpeg import ffmpeg_version
from bot.runtime import BOT_DATA_KEY, Runtime
from bot.utils import budget_note, disk_free_bytes, human_size
from config import PROJECT_NAME, VERSION, Settings

log = logging.getLogger("bot.web")

SECRET_HEADER = "X-Telegram-Bot-Api-Secret-Token"
MAX_PAYLOAD_BYTES = 2 * 1024 * 1024


#: ``ffmpeg -version`` is a subprocess; platforms poll /health every few seconds, so the
#: answer is cached for a while instead of being re-derived on every request.
_FFMPEG_CACHE: dict[str, object] = {"version": None, "checked_at": 0.0}
_FFMPEG_TTL = 300.0


def _is_owner_request(request: web.Request, settings: Settings, runtime: Runtime) -> bool:
    """Whether this request may see the detailed payload.

    The health endpoint has to stay public for the platform's probe, so it answers with a
    minimal body (``ok``, version, uptime) to everyone and the operator's detail only when the
    secret header is presented. Without a ``WEBHOOK_SECRET`` there is nothing to check against,
    so the detail stays closed rather than open.
    """
    expected = settings.webhook_secret
    if not expected:
        return False
    return request.headers.get(SECRET_HEADER, "") == expected


def create_web_app(application: Application, settings: Settings) -> web.Application:
    """Build the aiohttp application served by the web-service deployment."""
    runtime: Runtime = application.bot_data[BOT_DATA_KEY]
    started = time.monotonic()
    health_lock = asyncio.Lock()

    async def cached_ffmpeg_version() -> str | None:
        now = time.monotonic()
        cached = _FFMPEG_CACHE["version"]
        if cached and now - float(_FFMPEG_CACHE["checked_at"]) < _FFMPEG_TTL:
            return str(cached)
        async with health_lock:
            now = time.monotonic()
            cached = _FFMPEG_CACHE["version"]
            if cached and now - float(_FFMPEG_CACHE["checked_at"]) < _FFMPEG_TTL:
                return str(cached)
            version = await ffmpeg_version(settings)
            _FFMPEG_CACHE["version"] = version
            _FFMPEG_CACHE["checked_at"] = time.monotonic()
            return version

    async def root(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "service": PROJECT_NAME,
                "version": VERSION,
                "mode": "webhook" if settings.runs_webhook else "polling",
                "api_mode": settings.api_mode,
                "upload_capability_mb": settings.upload_capability_mb,
                "upload_transport": settings.transport,
            }
        )

    async def health(request: web.Request) -> web.Response:
        """Liveness for the platform, detail for the operator.

        Deliberately shallow: the probe answer must not depend on FFmpeg, on Telegram or on
        the MTProto client — a service that is up is up, and a deep check that fails turns a
        healthy deployment into a failed one.
        """
        ffmpeg = await cached_ffmpeg_version()
        uploader = runtime.uploader
        payload: dict = {
            "ok": True,
            "service": PROJECT_NAME,
            "version": VERSION,
            "uptime_seconds": round(time.monotonic() - started),
        }
        if _is_owner_request(request, settings, runtime):
            payload.update(
                {
                    "mode": "webhook" if settings.runs_webhook else "polling",
                    "api_mode": settings.api_mode,
                    "upload_capability_mb": settings.upload_capability_mb,
                    "bot_api_capability_mb": settings.bot_api_capability_mb,
                    "mtproto_capability_mb": settings.mtproto_capability_mb
                    if settings.mtproto_ready
                    else 0,
                    "upload_transport": settings.transport,
                    # Informational only: the port must answer even while MTProto is still
                    # connecting, or a platform would report the deploy as failed.
                    "mtproto": getattr(uploader, "state", "off"),
                    "mtproto_error": getattr(uploader, "last_error", ""),
                    "jobs": runtime.manager.stats(settings),
                    "runtime": {
                        "ffmpeg": ffmpeg or "missing",
                        "cpu_budget": budget_note(),
                        "free_disk": human_size(disk_free_bytes(settings.download_dir)),
                        "concurrent_jobs": settings.max_concurrent_jobs,
                    },
                }
            )
        return web.json_response(payload, status=200)

    async def stats(_request: web.Request) -> web.Response:
        return web.json_response(
            {
                "ok": True,
                "jobs": runtime.manager.snapshot(),
                "counters": runtime.manager.stats(settings),
                "cached_tracks": len(runtime.tracks),
                # The two numbers that answer "is the bot actually listening?": the
                # application processing updates, and how many are waiting in its queue.
                "application_running": bool(getattr(application, "running", False)),
                "queued_updates": application.update_queue.qsize(),
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
        log.debug("Update %s received (%s)", update.update_id, payload.get("message", {}).get("text", "")[:40])
        await application.update_queue.put(update)
        return web.json_response({"ok": True})

    app = web.Application()
    app.router.add_get("/", root)
    app.router.add_get("/health", health)
    app.router.add_get("/healthz", health)
    app.router.add_get("/stats", stats)
    app.router.add_post(f"/{settings.webhook_path.strip('/')}", webhook)
    return app
