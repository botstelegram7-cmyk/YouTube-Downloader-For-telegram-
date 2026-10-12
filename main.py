#!/usr/bin/env python3
"""Entry point.

python main.py                 # run (webhook when WEBHOOK_URL is set, else polling)
python main.py --polling       # force long polling (local development)
python main.py --check         # validate the configuration and dependencies
python main.py --check --online  # also call getMe with the configured token
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import json
import logging
import os
import sys
from pathlib import Path

from bot import texts
from bot.ffmpeg import ffmpeg_version
from bot.logging_setup import setup_logging
from bot.utils import disk_free_bytes, human_size
from config import PROJECT_NAME, VERSION, ConfigError, Settings, load_settings

log = logging.getLogger("bot.main")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="youtube-downloader-bot",
        description=f"{PROJECT_NAME} — v{VERSION}",
    )
    parser.add_argument("--check", action="store_true", help="validate configuration and dependencies, then exit")
    parser.add_argument("--online", action="store_true", help="with --check: also verify the bot token with getMe")
    parser.add_argument("--polling", action="store_true", help="force long polling instead of the webhook")
    parser.add_argument("--webhook", action="store_true", help="force webhook mode (requires WEBHOOK_URL)")
    parser.add_argument("--print-config", action="store_true", help="print the effective configuration as JSON")
    parser.add_argument("--version", action="version", version=f"{PROJECT_NAME} {VERSION}")
    return parser.parse_args(argv)


def apply_overrides(settings: Settings, args: argparse.Namespace) -> Settings:
    changes = {}
    if args.polling:
        changes["use_polling"] = True
    if args.webhook:
        if not settings.webhook_url:
            raise ConfigError(["--webhook was given but WEBHOOK_URL is empty"])
        changes["use_polling"] = False
    return dataclasses.replace(settings, **changes) if changes else settings


async def dependency_report(settings: Settings) -> dict[str, str]:
    """Which pieces of the toolchain are actually present."""
    report: dict[str, str] = {}
    ffmpeg = await ffmpeg_version(settings)
    report["ffmpeg"] = ffmpeg or "MISSING"

    try:
        from yt_dlp import version as ytdlp_version

        report["yt_dlp"] = getattr(ytdlp_version, "__version__", "unknown")
    except Exception as exc:
        report["yt_dlp"] = f"MISSING ({exc})"

    try:
        import telegram

        report["python_telegram_bot"] = telegram.__version__
    except Exception:
        report["python_telegram_bot"] = "MISSING"

    return report


def check_download_dir(settings: Settings) -> str:
    target = Path(settings.download_dir)
    try:
        target.mkdir(parents=True, exist_ok=True)
        probe = target / ".write-test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        return f"NOT WRITABLE ({exc})"
    return f"writable, {human_size(disk_free_bytes(target))} free"


async def run_check(settings: Settings, *, online: bool) -> int:
    """Pre-flight: fail loudly here instead of mysteriously in production."""
    problems = settings.validate()
    warnings = settings.warnings()
    deps = await dependency_report(settings)

    print(texts.banner(PROJECT_NAME, VERSION, settings.summary()))
    print(json.dumps(settings.summary(), indent=2))
    print(f"\nDownload directory : {check_download_dir(settings)}")
    print(f"Toolchain          : {', '.join(f'{k} {v}' for k, v in deps.items())}")

    for note in warnings:
        print(f"NOTE    {note}")
    for problem in problems:
        print(f"PROBLEM {problem}")

    if "MISSING" in deps.get("ffmpeg", ""):
        problems.append("FFmpeg is required to mux video and audio.")
    if "MISSING" in deps.get("yt_dlp", ""):
        problems.append("yt-dlp is required to download media.")

    if online and not problems:
        try:
            from telegram import Bot

            bot = Bot(token=settings.bot_token, base_url=f"{settings.api_base}/bot")
            me = await bot.get_me()
            print(f"ONLINE  connected as @{me.username} (id {me.id})")
            await bot.shutdown()
        except Exception as exc:
            problems.append(f"Telegram rejected the token: {exc}")

    if problems:
        print(f"\n{len(problems)} problem(s) found — fix them before deploying.")
        return 2
    print("\nEverything checks out. ✅")
    return 0


async def _run(settings: Settings) -> int:
    from bot.runner import run_bot

    await run_bot(settings)
    return 0


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    setup_logging(os.getenv("LOG_LEVEL", "INFO"), pretty=os.getenv("LOG_PRETTY", "true").lower() != "false")

    try:
        settings = apply_overrides(load_settings(), args)
    except ConfigError as exc:
        logging.getLogger("bot").error(str(exc))
        return 2

    if args.print_config:
        print(json.dumps(settings.summary(), indent=2))
        return 0

    if args.check:
        return asyncio.run(run_check(settings, online=args.online))

    problems = settings.validate()
    if problems:
        logging.getLogger("bot").error(str(ConfigError(problems)))
        return 2

    print(texts.banner(PROJECT_NAME, VERSION, settings.summary()))
    for note in settings.warnings():
        log.warning("%s", note)

    try:
        return asyncio.run(_run(settings))
    except ConfigError as exc:
        log.error("%s", exc)
        return 2
    except KeyboardInterrupt:  # pragma: no cover - handled by the runner
        return 0


if __name__ == "__main__":
    sys.exit(main())
