"""Every user-facing string lives here, so the bot's voice stays consistent.

House style: short lines, an emoji that carries meaning rather than decoration, figures
bolded, and never a promise the code does not keep (sizes and limits especially).
"""

from __future__ import annotations

from bot.utils import html_escape as esc
from config import MT_UPLOAD_LIMIT_MB

COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Welcome screen and shortcuts"),
    ("help", "How to use the bot, step by step"),
    ("status", "What is running now, plus limits and versions"),
    ("cancel", "Stop your download, conversion and upload"),
    ("limits", "Why files are capped and how to raise the cap"),
    ("id", "Show your chat id (for OWNER_USER_ID)"),
    ("about", "Version, engine and credits"),
)


def banner(name: str, version: str, settings_summary: dict) -> str:
    """Console introduction printed when the process starts."""
    mode = settings_summary.get("mode")
    lines = [
        "",
        "═" * 64,
        f"  {name}  ·  v{version}".ljust(64),
        "═" * 64,
        f"  Engine     : {settings_summary.get('engine')} ({settings_summary.get('typetube_host')})",
        f"  Telegram   : {settings_summary.get('api_mode')} ({settings_summary.get('api_base')})",
        f"  Upload cap : {settings_summary.get('upload_limit_mb')} MB  (via {_transport_label(settings_summary)})",
        f"  Mode       : {mode}" + (f" → {settings_summary.get('webhook_url')}" if mode == "webhook" else ""),
        f"  FFmpeg     : {settings_summary.get('ffmpeg')}",
        f"  Parallel   : {settings_summary.get('max_concurrent_jobs')} job(s)"
        f"  ·  default quality {settings_summary.get('default_quality')}",
        f"  Downloads  : {settings_summary.get('download_dir')}",
        "═" * 64,
        "",
    ]
    return "\n".join(lines)


def _transport_label(summary: dict) -> str:
    """How big files will travel, in words."""
    if summary.get("upload_transport") == "mtproto":
        cap = summary.get("mtproto_cap_mb") or 0
        return f"Telegram session (MTProto), up to {cap} MB"
    if (summary.get("api_upload_cap_mb") or 0) > 50:
        return f"local Bot API server, up to {summary.get('api_upload_cap_mb')} MB"
    return "cloud Bot API, up to 50 MB"


def welcome(name: str, version: str) -> str:
    """``/start`` — the introduction users see first."""
    return (
        f"👋 <b>Welcome to {esc(name)}</b>\n"
        f"<i>v{esc(version)} · TypeTube engine · FFmpeg delivery</i>\n"
        "\n"
        "Send a YouTube link and I will show you the qualities that video really has, "
        "download the one you pick and send it back as a <b>playable video</b>.\n"
        "\n"
        "<b>What you get</b>\n"
        "• 🎬 MP4 with sound, thumbnail and a clean caption\n"
        "• 🎵 Audio only, as M4A\n"
        "• ⚡️ Downloads run on parallel range workers, so no throttling\n"
        "\n"
        "<b>Good to know</b>\n"
        "• One download at a time — a new link replaces the running one\n"
        "• <code>/cancel</code> stops everything instantly\n"
        "• Buttons are colour-coded: green is recommended, red cancels\n"
    )


def help_text() -> str:
    return (
        "📖 <b>How to use this bot</b>\n"
        "\n"
        "<b>1 · Send a link</b>\n"
        "Paste any YouTube link — <code>youtube.com/watch</code>, <code>youtu.be</code>, "
        "<code>/shorts</code>, <code>/live</code> or just the 11-character video id.\n"
        "\n"
        "<b>2 · Pick a quality</b>\n"
        "I list the qualities that video actually has, from 144p up to its maximum, and mark "
        "the best one in green. Audio-only is one tap away when you want just the sound.\n"
        "\n"
        "<b>3 · Wait for the file</b>\n"
        "A status message shows download, mux and upload progress, with a red "
        "<b>Cancel download</b> button the whole time.\n"
        "\n"
        "<b>Tips</b>\n"
        "• <code>/status</code> — what is running, plus limits and versions\n"
        "• <code>/limits</code> — why there is a size cap and how to raise it\n"
        "• Videos are streamed by Telegram: play them while they download\n"
        "\n"
        "<i>Weaker connections: pick a lower quality — it finishes faster and still plays.</i>"
    )


def about_text(version: str, engine: str = "TypeTube") -> str:
    return (
        "ℹ️ <b>About</b>\n"
        "\n"
        f"<b>Version</b>: {esc(version)}\n"
        f"<b>Engine</b>: {esc(engine)}\n"
        "<b>Delivery</b>: FFmpeg mux, faststart, thumbnail, streaming video\n"
        "<b>Built with</b>: python-telegram-bot, TypeTube, FFmpeg\n"
        "\n"
        "<i>Only download what you own or are allowed to save.</i>"
    )


def commands_list() -> str:
    width = max(len(cmd) for cmd, _ in COMMANDS)
    rows = "\n".join(f"/{cmd.ljust(width)}  {esc(text)}" for cmd, text in COMMANDS)
    return f"<b>Commands</b>\n<pre>{rows}</pre>"


def startup_report(version: str, summary: dict, warnings: list[str]) -> str:
    """Sent to the notify chat when the bot comes online."""
    lines = [
        "🚀 <b>Bot online</b>",
        f"<i>v{esc(version)}</i>",
        "",
        f"<b>Engine</b>: {esc(str(summary.get('engine')))}",
        f"<b>Telegram API</b>: {esc(str(summary.get('api_mode')))}",
        f"<b>Upload cap</b>: {summary.get('upload_limit_mb')} MB · {esc(_transport_label(summary))}",
        f"<b>Mode</b>: {esc(str(summary.get('mode')))}",
        f"<b>Jobs in parallel</b>: {summary.get('max_concurrent_jobs')}",
    ]
    if warnings:
        lines += ["", "⚠️ <b>Notes</b>"]
        lines += [f"• {esc(note)}" for note in warnings[:4]]
    lines += ["", commands_list()]
    return "\n".join(lines)


def analyzing(url: str) -> str:
    return "🔍 <b>Reading the link…</b>\n<i>Resolving streams with TypeTube.</i>"


def choose_quality(title: str, duration: str, uploader: str, limit_mb: int, *, replaces_running: bool = False) -> str:
    header = "♻️ <i>Your running download is replaced when you pick.</i>\n\n" if replaces_running else ""
    lines = [
        header + f"🎬 <b>{esc(title[:90])}</b>",
    ]
    if uploader:
        lines.append(f"📺 {esc(uploader[:60])}")
    lines.append(f"⏱ {esc(duration)}  ·  ⬆️ up to <b>{limit_mb} MB</b> per file")
    lines.append("")
    lines.append("<i>Pick a quality — green is the best available.</i>")
    return "\n".join(lines)


def job_started(title: str, quality: str) -> str:
    return f"⬇️ <b>Starting</b>\n<i>{esc(title[:80])}</i>\n<b>{esc(quality)}</b>"


def progress_text(
    title: str,
    quality: str,
    phase: str,
    percent: float,
    bar: str,
    *,
    detail: str = "",
    eta: str = "",
) -> str:
    lines = [
        f"{phase} — <b>{esc(quality)}</b>",
        f"<i>{esc(title[:70])}</i>",
        "",
        f"<code>{bar}</code> <b>{percent:5.1f}%</b>",
    ]
    if detail:
        lines.append(detail)
    if eta and eta != "—":
        lines.append(f"⏳ {esc(eta)} remaining")
    return "\n".join(lines)


def done_caption(
    *,
    title: str,
    quality: str,
    resolution: str,
    duration: str,
    size: str,
    url: str,
    kind: str = "video",
) -> str:
    icon = "🎵" if kind == "audio" else "🎬"
    lines = [
        f"{icon} <b>{esc(title[:150])}</b>",
        f"🎚 {esc(quality)}"
        + (f"  ·  {esc(resolution)}" if resolution else "")
        + f"  ·  {esc(duration)}  ·  {esc(size)}",
    ]
    if url:
        lines.append(f'🔗 <a href="{esc(url)}">Source</a>')
    return "\n".join(lines)


def too_large(size_mb: float, summary: dict) -> str:
    """The refusal message when a finished file is simply too big for this setup."""
    limit = summary.get("upload_limit_mb")
    transport = summary.get("upload_transport")

    if transport == "mtproto":
        advice = (
            f"Even the MTProto session tops out at {summary.get('upload_cap_mb')} MB per file (Telegram's own "
            "ceiling for accounts). Try a lower quality or an audio-only download."
        )
    else:
        advice = (
            "Pick a lower quality, or enable big uploads: get <code>api_id</code>/<code>api_hash</code> from "
            "https://my.telegram.org, run <code>python scripts/make_session.py</code> once, then set "
            "<code>TELEGRAM_API_ID</code>, <code>TELEGRAM_API_HASH</code> and "
            f"<code>TELEGRAM_SESSION_STRING</code> — that raises the cap to "
            f"{summary.get('mtproto_cap_mb') or MT_UPLOAD_LIMIT_MB} MB."
        )

    return (
        "⚠️ <b>Too large to send</b>\n"
        f"The result would be about <b>{size_mb:.0f} MB</b> and this bot can send <b>{limit} MB</b>.\n\n{advice}"
    )


def cancelled() -> str:
    return "🛑 <b>Cancelled.</b>\nStopped the download, the muxing and the upload, and removed the files."


def nothing_to_cancel() -> str:
    return "Nothing is running for you right now."


def picker_dismissed() -> str:
    return "👍 Dismissed. Send another link whenever you like."


def replaced_previous(title: str) -> str:
    return f"♻️ Cancelled your previous request to start <b>{esc(title[:70])}</b>."


def not_youtube(host: str) -> str:
    return (
        "🤔 <b>That link is not from YouTube</b>\n"
        f"<i>{esc(host)}</i>\n\n"
        "This bot downloads with the TypeTube engine, which serves YouTube only.\n"
        "Send a <code>youtube.com</code>, <code>youtu.be</code>, <code>/shorts</code> or "
        "<code>/live</code> link."
    )


def not_supported(text: str) -> str:
    return (
        "🔗 <b>Send me a YouTube link</b>\n"
        "Paste a video URL (<code>youtube.com/watch?v=…</code>, <code>youtu.be/…</code>, a Short or a live link) "
        "or an 11-character video id.\n"
        f"\n<i>{esc(text[:120])}</i>"
    )


def resolve_failed(url: str, reason: str) -> str:
    return (
        "😕 <b>I could not read that video</b>\n"
        f"<i>{esc(reason)}</i>\n\n"
        "• Check that the video plays in a browser\n"
        "• Private, age-restricted or region-locked videos cannot be resolved\n"
        "• If it keeps failing, the TypeTube cluster may be busy — try again in a minute"
    )


def no_streams() -> str:
    return (
        "😕 <b>No downloadable streams</b>\n"
        "That video reported no usable video or audio track — it may be a live stream that just started, "
        "a premiere, or a page that needs a sign-in."
    )


def too_long(duration: float, limit_minutes: int) -> str:
    """``MAX_DURATION_MINUTES`` refused this video before anything was downloaded."""
    from bot.utils import human_duration

    return (
        "⏱ <b>That video is too long</b>\n"
        f"It runs for <b>{esc(human_duration(duration))}</b> and this bot accepts up to "
        f"<b>{limit_minutes} min</b>.\n\n"
        "<i>An operator can raise MAX_DURATION_MINUTES if the server has room.</i>"
    )


def limits_text(summary: dict) -> str:
    """``/limits`` — the honest number, and the way to raise it."""
    limit = summary.get("upload_limit_mb")
    transport = summary.get("upload_transport")
    cap = summary.get("upload_cap_mb") or limit

    if transport == "mtproto":
        how = (
            f"Large files are sent through a <b>Telegram user session</b> (MTProto), because the Bot API stops at "
            f"{summary.get('api_upload_cap_mb')} MB. This account can upload <b>{cap} MB</b> per file — Telegram's "
            "ceiling is 2000 MB, or 4000 MB with Premium.\n\n"
            f"<i>Files up to {summary.get('api_upload_cap_mb')} MB are still delivered straight through the Bot API, "
            "which is faster.</i>"
        )
    elif (summary.get("api_upload_cap_mb") or 0) > 50:
        how = (
            f"This bot talks to a <b>self-hosted Bot API server</b>, so files up to <b>{cap} MB</b> can be sent — "
            "Telegram's ceiling for bots.\n\n"
            "<i>Premium's 4 GB applies to people uploading in the app, not to bots: to go beyond 2000 MB, add a "
            "Premium user session instead.</i>"
        )
    else:
        how = (
            f"Currently <b>{limit} MB</b> — the ceiling for bots on Telegram's cloud API.\n\n"
            "<b>How to raise it</b>\n"
            "1 · Get <code>api_id</code> / <code>api_hash</code> from https://my.telegram.org\n"
            "2 · Run <code>python scripts/make_session.py</code> once to log in\n"
            "3 · Set <code>TELEGRAM_API_ID</code>, <code>TELEGRAM_API_HASH</code> and "
            "<code>TELEGRAM_SESSION_STRING</code>\n\n"
            "Result: up to <b>2000 MB</b> per file, 4000 MB if that account has Premium.\n"
            "<i>The alternative is a self-hosted Bot API server (<code>TELEGRAM_API_BASE</code>).</i>"
        )

    return (
        "📦 <b>Size limits</b>\n\n"
        f"<b>Upload cap</b>: {limit} MB\n"
        f"<b>Transport</b>: {esc(_transport_label(summary))}\n\n"
        f"{how}\n\n"
        "<b>Why a cap at all?</b>\n"
        "Every file is downloaded, muxed and uploaded through this server; the cap keeps a single request from "
        "filling its disk."
    )


def status_text(summary: dict, active: list[str], free_disk: str) -> str:
    lines = [
        "📊 <b>Status</b>",
        "",
        f"<b>Engine</b>: {esc(str(summary.get('engine')))}",
        f"<b>Upload cap</b>: {summary.get('upload_limit_mb')} MB · {esc(_transport_label(summary))}",
        f"<b>Parallel jobs</b>: {summary.get('max_concurrent_jobs')}",
        f"<b>Free disk</b>: {esc(free_disk)}",
        f"<b>Mode</b>: {esc(str(summary.get('mode')))}",
    ]
    if active:
        lines += ["", "<b>Running now</b>"] + [f"• {esc(item)}" for item in active]
    else:
        lines += ["", "<i>Nothing running.</i>"]
    return "\n".join(lines)


def job_failed(reason: str) -> str:
    return f"❌ <b>That download failed</b>\n\n{esc(reason)}"


def unsupported_runtime(reason: str) -> str:
    return (
        "🛠 <b>The server is not ready</b>\n"
        f"{esc(reason)}\n\n"
        "<i>An operator needs to fix this — the bot itself is running.</i>"
    )
