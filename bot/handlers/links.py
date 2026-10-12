"""Incoming messages: turn a link into something the user can act on.

Three kinds of link arrive, and each gets the treatment that fits it:

* **YouTube** — the engine resolves the real streams and the user picks a quality.
* **Torrent / magnet** — the file list is fetched first, and the user picks which file inside
  the torrent to download (a season pack is 30 GB; one episode is 400 MB).
* **Direct URL** — the server is asked what it is, then the user confirms.

Inspection happens in a background task so the update is acknowledged immediately — important
in webhook mode, where Telegram retries when a handler is slow.
"""

from __future__ import annotations

import asyncio
import logging

from telegram import Update
from telegram.constants import ChatType, ParseMode
from telegram.ext import ContextTypes

from bot import texts
from bot.cache import PendingFetch
from bot.errors import BotError
from bot.fetch import probe_link
from bot.keyboards import direct_keyboard, quality_keyboard, torrent_keyboard
from bot.qualities import build_options, default_option
from bot.runtime import Runtime, runtime
from bot.sources import DIRECT, TORRENT, Source, classify
from bot.torrent import TorrentError, parse_torrent_file
from bot.typetube import Track, resolve
from bot.utils import human_duration
from config import Settings

log = logging.getLogger("bot.handlers.links")

#: Inspector timeout: faster than Telegram's own webhook timeout.
INSPECT_TIMEOUT = 45.0


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Entry point for every text/caption message."""
    message = update.effective_message
    chat = update.effective_chat
    user = update.effective_user
    if message is None or chat is None or user is None:
        return

    text = (message.text or message.caption or "").strip()
    rt = runtime(context)
    source, refusal = classify(text, rt.settings)

    if source is None:
        # Only nudge in private chats, and only for something that looks like an
        # attempt at a link — never answer ordinary conversation in groups.
        if chat.type == ChatType.PRIVATE and text and not text.startswith("/"):
            if refusal:
                log.info("Refused a link from user %s: %s", user.id, refusal)
                await message.reply_text(texts.link_refused(refusal), parse_mode=ParseMode.HTML)
            else:
                await message.reply_text(texts.not_supported(text), parse_mode=ParseMode.HTML)
        return

    # Fairness delay: the bot is open to everyone, so one person cannot flood the queue.
    # The owner is exempt (an operator testing their own bot should never be told to wait).
    remaining = rt.access.cooldown_remaining(user.id)
    if remaining > 0:
        await message.reply_text(texts.cooldown(remaining), parse_mode=ParseMode.HTML)
        return
    rt.access.mark_request(user.id)

    waiting = (
        texts.analyzing_torrent(source.name or source.label)
        if source.is_torrent
        else texts.analyzing(source.url)
        if source.is_youtube
        else texts.analyzing_link(source.label)
    )
    status = await message.reply_text(waiting, parse_mode=ParseMode.HTML, disable_web_page_preview=True)

    task = asyncio.create_task(
        _offer(
            rt=rt,
            settings=rt.settings,
            chat_id=chat.id,
            user_id=user.id,
            source=source,
            status=status,
            reply_to=message.message_id,
        )
    )
    rt.track(task)


async def _offer(
    *,
    rt: Runtime,
    settings: Settings,
    chat_id: int,
    user_id: int,
    source: Source,
    status,
    reply_to: int | None,
) -> None:
    """Work out what the link offers and show the matching picker."""
    if source.is_youtube:
        await _offer_youtube(
            rt=rt, settings=settings, chat_id=chat_id, user_id=user_id, source=source, status=status
        )
    elif source.is_torrent:
        await _offer_torrent(rt=rt, settings=settings, user_id=user_id, source=source, status=status)
    else:
        await _offer_direct(rt=rt, settings=settings, user_id=user_id, source=source, status=status)


# -- YouTube -------------------------------------------------------------------- #
async def _offer_youtube(*, rt: Runtime, settings: Settings, chat_id: int, user_id: int, source: Source, status) -> None:
    """Ask TypeTube what the video offers and show the qualities it really has."""
    try:
        track: Track = await asyncio.wait_for(resolve(source.url, settings), timeout=INSPECT_TIMEOUT)
    except asyncio.TimeoutError:
        await _safe_edit(status, texts.resolve_failed(source.url, "Reading the video took too long."))
        return
    except BotError as exc:
        await _safe_edit(status, texts.resolve_failed(source.url, exc.reason))
        return
    except Exception as exc:
        log.exception("Unexpected resolve failure for %s", source.url[:80])
        await _safe_edit(status, texts.resolve_failed(source.url, str(exc)[:200]))
        return

    limit_minutes = settings.max_duration_minutes
    if limit_minutes and track.duration > limit_minutes * 60:
        await _safe_edit(status, texts.too_long(track.duration, limit_minutes))
        return

    options = build_options(track, settings, capability_bytes=settings.upload_capability_bytes)
    if not options:
        await _safe_edit(status, texts.no_streams())
        return

    token = rt.tracks.put(track, user_id=user_id)
    recommended = default_option(options, settings)

    body = texts.choose_quality(
        track.title,
        human_duration(track.duration),
        track.author,
        settings.upload_capability_mb,
        replaces_running=rt.manager.for_user(user_id) is not None,
    )

    try:
        await status.edit_text(
            body,
            parse_mode=ParseMode.HTML,
            disable_web_page_preview=True,
            reply_markup=quality_keyboard(options, token, recommended=recommended),
        )
    except Exception as exc:
        log.debug("Could not show the quality keyboard: %s", exc)


# -- torrents -------------------------------------------------------------------- #
async def _offer_torrent(*, rt: Runtime, settings: Settings, user_id: int, source: Source, status) -> None:
    """Download the torrent's metadata (only the metadata), then list what is inside."""
    work = settings.download_dir / f"torrent-{user_id}"
    try:
        torrent_path = await asyncio.wait_for(
            _torrent_metadata(rt=rt, source=source, work=work, settings=settings),
            timeout=settings.torrent_metadata_timeout + 30,
        )
    except asyncio.TimeoutError:
        await _safe_edit(
            status,
            texts.link_refused(
                "No peers handed over the torrent's file list in time. Try again, or send the "
                ".torrent file itself as a document."
            ),
        )
        return
    except BotError as exc:
        # Every refusal is named in the log: a handler that answers "sorry" and says nothing
        # else is impossible to support.
        log.warning("Torrent metadata failed for %s: %s", source.url[:100], exc.reason)
        await _safe_edit(status, texts.link_refused(exc.reason))
        return
    except Exception as exc:
        log.exception("Torrent metadata failed for %s", source.url[:80])
        await _safe_edit(status, texts.link_refused(str(exc)[:200]))
        return

    try:
        info = parse_torrent_file(torrent_path)
    except TorrentError as exc:
        await _safe_edit(status, texts.link_refused(exc.reason))
        return

    candidates = info.candidates(8)
    entry = PendingFetch(
        kind=TORRENT,
        source=source,
        user_id=user_id,
        label=info.name,
        title=info.name,
        total_size=info.total_size,
        files=[(item.index, item.path, item.size) for item in candidates],
        torrent_path=torrent_path,
        detail=f"{len(info.files)} file(s)",
    )
    token = rt.pending.put(entry)
    log.info("Offered %s file(s) from torrent %r to user %s", len(candidates), info.name[:60], user_id)

    hint = ""
    if not candidates:
        await _safe_edit(status, texts.link_refused("That torrent contains no files."))
        return
    if len(candidates) == 1:
        hint = "One file inside — the biggest is shown first."
    await _safe_edit(
        status,
        texts.torrent_files(info.name, entry.files, info.total_size, hint=hint),
        markup=torrent_keyboard(entry.files, token),
    )


async def _torrent_metadata(*, rt: Runtime, source: Source, work, settings: Settings):
    from bot.fetch import fetch_torrent_metadata

    work.mkdir(parents=True, exist_ok=True)
    return await fetch_torrent_metadata(
        source=source,
        destination=work,
        settings=settings,
        registry=rt.registry,
        timeout=settings.torrent_metadata_timeout,
    )


# -- direct links ---------------------------------------------------------------- #
async def _offer_direct(*, rt: Runtime, settings: Settings, user_id: int, source: Source, status) -> None:
    """Ask the server what the file is, then offer it."""
    probe = await probe_link(source.url)
    name = source.name or probe.filename or source.label
    entry = PendingFetch(
        kind=DIRECT,
        source=Source(kind=DIRECT, url=source.url, label=source.label, name=name),
        user_id=user_id,
        label=source.label,
        title=name,
        total_size=int(probe.total or 0),
        detail=probe.content_type,
    )
    token = rt.pending.put(entry)

    note = ""
    if not probe.reachable:
        if probe.error.startswith("HTTP 403") or probe.error.startswith("HTTP 401"):
            note = "The server refused my probe — it may still allow the download itself."
        elif probe.error:
            note = f"The server did not answer my probe ({probe.error}). Trying anyway is up to you."
    if probe.total and probe.total > settings.upload_capability_bytes:
        # Telegram's ceiling is known before a single byte is fetched: say so now, honestly.
        note = (
            f"⚠️ That file looks larger than the {settings.upload_capability_mb} MB Telegram "
            "ceiling for this bot's transport."
        )
    await _safe_edit(
        status,
        texts.direct_link(
            source.label,
            name,
            int(probe.total or 0),
            probe.content_type,
            note=note,
        ),
        markup=direct_keyboard(token),
    )


# -- shared ---------------------------------------------------------------------- #
async def _safe_edit(message, text: str, *, markup=None) -> None:
    try:
        await message.edit_text(
            text, parse_mode=ParseMode.HTML, disable_web_page_preview=True, reply_markup=markup
        )
    except Exception as exc:
        log.debug("Could not edit the status message: %s", exc)


async def reoffer(
    *,
    rt: Runtime,
    chat_id: int,
    user_id: int,
    source: Source,
    status_message,
) -> str:
    """Re-resolve a link and put a fresh picker on ``status_message``.

    Used by the "Try again" button on a failed job: YouTube links are re-read from the
    engine, torrents re-read their metadata, direct links are re-probed.
    """
    if source.is_youtube:
        try:
            track = await asyncio.wait_for(resolve(source.url, rt.settings), timeout=INSPECT_TIMEOUT)
        except (asyncio.TimeoutError, BotError, Exception) as exc:
            return f"Could not read that link again: {str(exc)[:160]}"
        options = build_options(track, rt.settings, capability_bytes=rt.settings.upload_capability_bytes)
        if not options:
            return "That video has no downloadable streams any more."
        token = rt.tracks.put(track, user_id=user_id)
        await _safe_edit(
            status_message,
            texts.choose_quality(
                track.title,
                human_duration(track.duration),
                track.author,
                rt.settings.upload_capability_mb,
            ),
            markup=quality_keyboard(options, token, recommended=default_option(options, rt.settings)),
        )
        return ""

    await _offer(
        rt=rt,
        settings=rt.settings,
        chat_id=chat_id,
        user_id=user_id,
        source=source,
        status=status_message,
        reply_to=None,
    )
    return ""


__all__ = ["INSPECT_TIMEOUT", "on_link", "reoffer"]
