"""Every user-facing string lives here, so the bot's voice stays consistent."""

from __future__ import annotations

from bot.utils import html_escape as esc
from config import LOCAL_UPLOAD_LIMIT_MB

COMMANDS: tuple[tuple[str, str], ...] = (
    ("start", "Show the welcome screen and feature list"),
    ("help", "How to use the bot, step by step"),
    ("status", "What is running now, plus limits and versions"),
    ("cancel", "Stop your download, conversion and upload"),
    ("limits", "Why files are capped and how to raise the cap"),
    ("id", "Show your chat id (for STARTUP_NOTIFY_CHAT_ID)"),
    ("about", "Version, engine and credits"),
)


def banner(name: str, version: str, settings_summary: dict) -> str:
    """Console introduction printed when the process starts."""
    lines = [
        "",
        "═" * 62,
        f"  {name}  ·  v{version}".ljust(62),
        "═" * 62,
        f"  API        : {settings_summary.get('api_mode')} ({settings_summary.get('api_base')})",
        f"  Upload cap : {settings_summary.get('upload_limit_mb')} MB"
        f"  (Telegram limit for this API: {settings_summary.get('api_upload_cap_mb')} MB)",
        f"  Mode       : {settings_summary.get('mode')}"
        + (f" → {settings_summary.get('webhook_url')}" if settings_summary.get("mode") == "webhook" else ""),
        f"  Engines    : yt-dlp + FFmpeg ({settings_summary.get('ffmpeg')})",
        f"  Parallel   : {settings_summary.get('max_concurrent_jobs')} job(s)"
        f"  ·  default quality {settings_summary.get('default_quality')}",
        f"  Downloads  : {settings_summary.get('download_dir')}",
        "═" * 62,
        "",
    ]
    return "\n".join(lines)


def welcome(name: str, version: str) -> str:
    """``/start`` — the introduction users see first."""
    return (
        f"👋 <b>Welcome to {esc(name)}</b>\n"
        f"<i>v{esc(version)} · powered by yt-dlp and FFmpeg</i>\n"
        "\n"
        "Send me a video link and I will show you the qualities that link actually offers, "
        "download the one you pick, and deliver a playable file with a thumbnail and a clean caption.\n"
        "\n"
        "<b>What I can send</b>\n"
        "• 🎬 Playable video (MP4, streaming-friendly, thumbnail attached)\n"
        "• 🎵 Audio only, as M4A/MP3 with title and artist tags\n"
        "• 📄 A plain document when a video container is refused\n"
        "\n"
        "<b>One request at a time</b>\n"
        "Send another link and I cancel the previous job for you automatically — no orphaned "
        "downloads, no wasted disk.\n"
    )


def help_text() -> str:
    """/help — the step-by-step usage guide."""
    return (
        "📖 <b>How to use this bot</b>\n"
        "\n"
        "<b>1 · Send a link</b>\n"
        "Paste a supported media URL (YouTube, Shorts, and the 1000+ other sites yt-dlp "
        "knows). I read the link's real formats first — nothing is downloaded yet.\n"
        "\n"
        "<b>2 · Pick a quality</b>\n"
        "You get inline buttons for the qualities that link <i>actually</i> has, each with an "
        "estimated size, plus <b>Best</b> and <b>Audio only</b> when they are available.\n"
        "\n"
        "<b>3 · Watch it happen</b>\n"
        "The status message shows live download, conversion and upload progress, then becomes "
        "the finished file.\n"
        "\n"
        "<b>Controls</b>\n"
        "• /cancel — stop everything for you and clean up\n"
        "• /status — what is running, queue, limits, versions\n"
        "• /limits — the file-size ceiling and how to raise it\n"
        "\n"
        "<b>Good to know</b>\n"
        "• Send a new link any time — the previous job for you is cancelled first\n"
        "• Filenames come from the video title; nothing is kept after upload\n"
        "• Age-restricted or members-only videos need a cookies file on the server\n"
    )


def about_text(version: str) -> str:
    return (
        f"ℹ️ <b>YouTube Downloader for Telegram</b>\n"
        f"Version <code>{esc(version)}</code>\n"
        "\n"
        "<b>Engine</b>: yt-dlp (extraction and download), FFmpeg (muxing, faststart, "
        "thumbnails, audio conversion)\n"
        "<b>Framework</b>: python-telegram-bot, asyncio\n"
        "<b>Storage</b>: temporary only — files are deleted right after upload\n"
        "\n"
        "Report a problem with /help and the exact link you used."
    )


def commands_list() -> str:
    from bot.utils import html_escape

    width = max(len(cmd) for cmd, _ in COMMANDS)
    rows = "\n".join(f"/{cmd.ljust(width)}  {html_escape(text)}" for cmd, text in COMMANDS)
    return f"<b>Commands</b>\n<pre>{rows}</pre>"


def startup_report(version: str, summary: dict, warnings: list[str]) -> str:
    """Sent to ``STARTUP_NOTIFY_CHAT_ID`` when the bot comes online."""
    lines = [
        "🚀 <b>Bot online</b>",
        f"<i>v{esc(version)}</i>",
        "",
        f"<b>API</b>: {esc(str(summary.get('api_mode')))}",
        f"<b>Upload cap</b>: {summary.get('upload_limit_mb')} MB"
        f" (Telegram allows {summary.get('api_upload_cap_mb')} MB here)",
        f"<b>Mode</b>: {esc(str(summary.get('mode')))}",
        f"<b>Jobs in parallel</b>: {summary.get('max_concurrent_jobs')}",
        f"<b>Default quality</b>: {esc(str(summary.get('default_quality')))}",
    ]
    if warnings:
        lines += ["", "⚠️ <b>Notes</b>"]
        lines += [f"• {esc(note)}" for note in warnings[:4]]
    lines += ["", commands_list()]
    return "\n".join(lines)


def analyzing(url: str) -> str:
    return f"🔎 Reading available formats…\n<code>{esc(url)}</code>"


def choose_quality(title: str, duration: str, size_limit_mb: int) -> str:
    return (
        f"🎯 <b>{esc(title)}</b>\n"
        f"⏱ {duration} · pick a quality below\n"
        f"\n<i>Files above {size_limit_mb} MB cannot be sent through the current API.</i>"
    )


def job_started(title: str, quality: str) -> str:
    return f"⬇️ <b>{esc(title)}</b>\nDownloading <b>{esc(quality)}</b>…"


def progress_text(
    *,
    title: str,
    quality: str,
    phase: str,
    percent: float | None,
    bar: str,
    size_line: str,
    speed: str,
    eta: str,
) -> str:
    """Live status message while a job runs."""
    head = {
        "download": "⬇️ <b>Downloading</b>",
        "convert": "🎛 <b>Converting</b>",
        "upload": "⬆️ <b>Uploading to Telegram</b>",
        "prepare": "🧰 <b>Preparing</b>",
    }.get(phase, "⏳ <b>Working</b>")

    lines = [f"{head} — {esc(quality)}", f"<i>{esc(title)}</i>", ""]
    if percent is None:
        lines.append(f"{bar}  <b>{esc(size_line)}</b>")
    else:
        lines.append(f"{bar}  <b>{percent:.1f}%</b>  ·  {esc(size_line)}")
        lines.append(f"⚡ {speed}  ·  ⏳ ETA {eta}")
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
    """Caption attached to the delivered file."""
    icon = {"video": "🎬", "audio": "🎵", "document": "📄"}.get(kind, "📦")
    details = [quality]
    if resolution and kind == "video":
        details.append(resolution)
    details.append(duration)
    details.append(size)
    return f"{icon} <b>{esc(title)}</b>\n🎚 {' · '.join(esc(part) for part in details)}\n🔗 {esc(url)}"


def too_large(size_mb: float, limit_mb: int, *, local_api: bool) -> str:
    if local_api:
        advice = "Pick a lower quality, or trim the video length."
    else:
        advice = (
            "Pick a lower quality. To send files this big, run a local Bot API server "
            "(docs/DEPLOYMENT.md → “Larger than 50 MB”) and point TELEGRAM_API_BASE at it."
        )
    return (
        f"⚠️ <b>That file is too large</b>\n"
        f"The result would be about <b>{size_mb:.0f} MB</b>, and this bot can send up to "
        f"<b>{limit_mb} MB</b>.\n\n{advice}"
    )


def cancelled() -> str:
    return "🛑 <b>Cancelled.</b>\nStopped your download, conversion and upload, and cleaned up the files."


def nothing_to_cancel() -> str:
    return "Nothing is running for you right now."


def replaced_previous(title: str) -> str:
    return f"♻️ Cancelled your previous request to start <b>{esc(title)}</b>."


def probe_failed(url: str, reason: str) -> str:
    return (
        "😕 <b>I could not read that link</b>\n"
        f"<code>{esc(url)}</code>\n\n"
        f"<i>{esc(reason)}</i>\n\n"
        "• Check that the link opens in a browser\n"
        "• Private, members-only and age-restricted videos need a cookies file on the server\n"
        "• Some sites block datacentre IP addresses"
    )


def not_supported(text: str) -> str:
    return (
        "🤔 <b>That does not look like a link</b>\n"
        "Send an http(s) link to a video, a Short, or an audio track.\n"
        f"\n<i>{esc(text[:180])}</i>"
    )


def blocked_url(reason: str) -> str:
    return f"🚫 <b>That link is not allowed</b>\n<i>{esc(reason)}</i>"


def no_formats() -> str:
    return (
        "😕 <b>No downloadable formats</b>\n"
        "The site reported no media streams for that link — it may be a live stream, "
        "a text page, or a page that needs a login."
    )


def limits_text(limit_mb: int, cap_mb: int, api_mode: str) -> str:
    if api_mode == "cloud Bot API":
        how = (
            f"Currently <b>{limit_mb} MB</b> — Telegram's standard limit for bots.\n\n"
            "<b>How to raise it</b>\n"
            "1 · Get <code>api_id</code>/<code>api_hash</code> from https://my.telegram.org\n"
            "2 · Run the official <code>telegram-bot-api</code> server with <code>--local</code>\n"
            f"3 · Point <code>TELEGRAM_API_BASE</code> at it (that lifts the cap to {LOCAL_UPLOAD_LIMIT_MB} MB)\n"
            "\nFull walkthrough, including the docker-compose file that does all three: "
            "<code>docs/DEPLOYMENT.md</code>."
        )
    else:
        how = (
            f"Currently <b>{limit_mb} MB</b> — your local Bot API server supports the same "
            f"{cap_mb} MB ceiling Telegram allows.\n"
            "Files are uploaded straight through it, so nothing is truncated."
        )
    return f"📦 <b>File size limit</b>\n\n{how}"


def status_text(summary: dict, active: list[str], free_disk: str) -> str:
    lines = [
        "📊 <b>Status</b>",
        "",
        f"<b>Mode</b>: {esc(str(summary.get('mode')))}",
        f"<b>API</b>: {esc(str(summary.get('api_mode')))}",
        f"<b>Upload cap</b>: {summary.get('upload_limit_mb')} MB",
        f"<b>Parallel jobs</b>: {summary.get('max_concurrent_jobs')}",
        f"<b>Free disk</b>: {free_disk}",
    ]
    if active:
        lines += ["", "<b>Running for you</b>"] + [f"• {esc(item)}" for item in active]
    else:
        lines += ["", "You have nothing running."]
    return "\n".join(lines)


def job_failed(reason: str) -> str:
    return f"❌ <b>That did not work</b>\n\n{esc(reason)}"


def unsupported_runtime(reason: str) -> str:
    return f"⚙️ <b>Missing a system dependency</b>\n\n{esc(reason)}"
