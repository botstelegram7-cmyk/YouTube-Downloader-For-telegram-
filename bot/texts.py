"""Every user-facing string lives here, so the bot's voice stays consistent.

House style: short lines, an emoji that carries meaning rather than decoration, figures
bolded, and never a promise the code does not keep (sizes and limits especially).
"""

from __future__ import annotations

from bot.utils import html_escape as esc
from config import BOT_API_UPLOAD_MB

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
        f"  Upload     : up to {settings_summary.get('upload_capability_mb')} MB per file"
        f"  ({_transport_label(settings_summary)})",
        f"  Mode       : {mode}"
        + (f" → {settings_summary.get('webhook')}" if mode == "webhook" else " (long polling)"),
        f"  FFmpeg     : {settings_summary.get('ffmpeg')}",
        f"  Parallel   : {settings_summary.get('max_concurrent_jobs')} job(s)"
        f"  ·  default quality {settings_summary.get('default_quality')}",
        f"  Downloads  : {settings_summary.get('download_dir')}",
        "═" * 64,
        "",
    ]
    return "\n".join(lines)


def _transport_label(summary: dict) -> str:
    """How a file travels, in words, with Telegram's own number attached."""
    if summary.get("upload_transport") == "mtproto":
        return f"MTProto with your bot token (Telegram allows {summary.get('mtproto_capability_mb')} MB)"
    if (summary.get("bot_api_capability_mb") or 0) > BOT_API_UPLOAD_MB:
        return f"a local Bot API server (Telegram allows {summary.get('bot_api_capability_mb')} MB there)"
    return (
        f"the Bot API, capped at {BOT_API_UPLOAD_MB} MB by Telegram itself — add TELEGRAM_API_ID and "
        "TELEGRAM_API_HASH to reach 2000 MB with the same bot token"
    )


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
        f"<b>Upload</b>: up to {summary.get('upload_capability_mb')} MB per file"
        f" · {esc(str(summary.get('upload_transport')))}",
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
    """The refusal when a finished file exceeds what Telegram will accept from this bot."""
    if summary.get("upload_transport") == "mtproto":
        body = (
            f"Telegram's ceiling for a bot over MTProto is <b>{summary.get('mtproto_capability_mb')} MB</b>, "
            "and this is protocol-level — no bot can send more. "
            "Pick a lower quality, or the audio-only option for very long videos."
        )
    else:
        body = (
            f"Telegram's Bot API accepts at most <b>{BOT_API_UPLOAD_MB} MB</b> per upload from a bot. "
            "That ceiling is Telegram's, not this bot's — nothing here lowers it, and it cannot be raised "
            "through the Bot API at all.\n\n"
            "<b>One setup change unlocks 2000 MB</b>\n"
            "Get <code>api_id</code> and <code>api_hash</code> from https://my.telegram.org (API development "
            "tools) and set them as <code>TELEGRAM_API_ID</code> / <code>TELEGRAM_API_HASH</code>. That lets "
            "this same bot upload over MTProto, Telegram's own protocol — <b>no login, no phone number, no "
            "session string</b>."
        )
    return (
        "⚠️ <b>Too large for Telegram</b>\n"
        f"The file is about <b>{size_mb:.0f} MB</b> and the limit here is "
        f"<b>{summary.get('upload_capability_mb')} MB</b>.\n\n{body}"
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
    """``/limits`` — what is Telegram's ceiling, what is the host's, and what is not limited."""
    capability = summary.get("upload_capability_mb")
    transport = summary.get("upload_transport")
    mtproto_ready = summary.get("mtproto") != "off"
    lines = [
        "📦 <b>Size limits</b>",
        "",
        "<b>1 · Downloading — not limited by this bot</b>",
        "The TypeTube engine streams the whole video: 4K, hours long, any size it offers. "
        "Nothing in this application caps the download.",
        "",
        "<b>2 · Uploading — Telegram decides</b>",
        f"Transport: {esc(_transport_label(summary))}",
        f"Largest file this bot can send right now: <b>{capability} MB</b>",
    ]
    if transport == "mtproto":
        lines += [
            "",
            "MTProto is Telegram's own protocol, authorised with the <b>bot token you already have</b>. "
            "Its ceiling for bots is 2000 MB — the biggest a bot can ever send, with or without Premium "
            "(Premium's 4 GB is for people, not bots).",
        ]
        # Honesty about the live connection: the ceiling above is what the protocol allows,
        # and this says whether that route is actually usable at this moment.
        state = summary.get("mtproto_state") or "ready"
        if state in ("connecting", "idle"):
            lines += [
                "",
                "<i>The MTProto client is still connecting. Files above "
                f"{summary.get('bot_api_capability_mb')} MB will wait for it.</i>",
            ]
        elif state in ("error", "off"):
            lines += [
                "",
                "<i>The MTProto client cannot connect right now, so uploads are limited to "
                f"{summary.get('bot_api_capability_mb')} MB until it does. The server log has the "
                "reason.</i>",
            ]
    else:
        lines += [
            "",
            "The Bot API stops bots at 50 MB. Adding two values raises it to 2000 MB:",
            "1 · https://my.telegram.org → API development tools → <code>api_id</code> / <code>api_hash</code>",
            "2 · Set <code>TELEGRAM_API_ID</code> and <code>TELEGRAM_API_HASH</code>",
            "",
            "<i>These identify the Telegram application; your existing BOT_TOKEN still authorises the bot, so "
            "there is no login, no phone number and no session string.</i>",
            "",
            "Running a local Bot API server (<code>TELEGRAM_API_BASE</code>) also reaches 2000 MB, but needs "
            "a second service.",
        ]
    if mtproto_ready and transport != "mtproto":
        lines += ["", "<i>MTProto is configured but not selected (UPLOAD_TRANSPORT=bot).</i>"]

    lines += [
        "",
        "<b>3 · The host — its own constraint</b>",
        f"The workspace keeps <b>{summary.get('min_free_disk_mb')} MB</b> free before starting a job; a job "
        "that would fill the disk is refused rather than half-finished, and partial files are deleted. "
        "Bigger files need a bigger disk — that is hosting, not policy.",
    ]
    return "\n".join(lines)


def status_text(summary: dict, active: list[str], free_disk: str) -> str:
    lines = [
        "📊 <b>Status</b>",
        "",
        f"<b>Engine</b>: {esc(str(summary.get('engine')))}",
        f"<b>Upload</b>: up to {summary.get('upload_capability_mb')} MB per file"
        f" · {esc(str(summary.get('upload_transport')))}",
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
