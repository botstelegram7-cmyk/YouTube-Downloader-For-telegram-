import asyncio
import logging
import os
import shutil
import tempfile
from pathlib import Path
from aiohttp import web
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import Application, CommandHandler, ContextTypes, MessageHandler, filters
from telegram.error import BadRequest
from config import BOT_TOKEN, DEFAULT_QUALITY, ALLOWED_QUALITIES, PORT, DOWNLOAD_TIMEOUT

logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO").upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("youtube_downloader")
active_users: set[int] = set()
# Telegram's hosted Bot API limits bot uploads to 50 MB per file.
BOT_API_UPLOAD_LIMIT_BYTES = 50_000_000

async def health(_: web.Request) -> web.Response:
    return web.json_response({"ok": True, "service": "youtube-downloader-bot"})

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            "🎬 YouTube Downloader\n\nSend a YouTube video URL to download it.\n\n"
            "/quality 720p — choose quality\n/help — help and limits\n"
            "Only download content you own or are authorized to save."
        )

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if update.effective_message:
        await update.effective_message.reply_text(
            f"Send a YouTube video/Shorts URL. Quality options: {', '.join(sorted(ALLOWED_QUALITIES))}.\n"
            f"Default quality: {context.user_data.get('quality', DEFAULT_QUALITY)}.\n"
            "Telegram's hosted Bot API has a 50 MB upload limit per file. If a video is larger, choose a lower quality.\\n"
            "The host must provide FFmpeg."
        )

async def quality_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    if not msg:
        return
    if not context.args or context.args[0].lower() not in ALLOWED_QUALITIES:
        await msg.reply_text("Usage: /quality 720p\nOptions: " + ", ".join(sorted(ALLOWED_QUALITIES)))
        return
    context.user_data["quality"] = context.args[0].lower()
    await msg.reply_text(f"Quality set to {context.user_data['quality']}.")

def is_youtube_url(value: str) -> bool:
    from urllib.parse import urlparse
    if len(value) > 2048:
        return False
    candidate = value.strip()
    if "://" not in candidate:
        candidate = "https://" + candidate
    try:
        p = urlparse(candidate)
        return p.scheme in {"http", "https"} and (p.hostname or "").lower() in {
            "youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"
        } and bool(p.path.strip("/"))
    except ValueError:
        return False

async def download_video(url: str, quality: str, output_dir: Path) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    executable = shutil.which("yt-dlp")
    if not executable:
        raise RuntimeError("yt-dlp is not installed on this server.")
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is missing. Install FFmpeg and restart the bot.")
    command = [
        executable, "--no-playlist", "--restrict-filenames",
        "-f", f"bv*[height<={quality.rstrip('p')}]+ba/b[height<={quality.rstrip('p')}]",
        "--merge-output-format", "mp4", "--output", str(output_dir / "%(title).100B-%(id)s.%(ext)s"),
        "--no-warnings", "--newline", url
    ]
    proc = await asyncio.create_subprocess_exec(*command, stdout=asyncio.subprocess.PIPE,
                                                stderr=asyncio.subprocess.STDOUT)
    try:
        output, _ = await asyncio.wait_for(proc.communicate(), timeout=DOWNLOAD_TIMEOUT)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"Download timed out after {DOWNLOAD_TIMEOUT} seconds.") from None
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode != 0:
        log.warning("yt-dlp exited %s: %s", proc.returncode, output.decode(errors="replace")[-1500:])
        raise RuntimeError("Download failed. The video may be unavailable, restricted, too large, or yt-dlp may need updating.")
    media = [p for p in output_dir.iterdir() if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mov"}]
    if not media:
        raise RuntimeError("No video file was produced. Try a lower quality.")
    result = max(media, key=lambda p: p.stat().st_size)
    return result

async def handle_link(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    msg = update.effective_message
    user = update.effective_user
    if not msg or not msg.text or not user:
        return
    url = msg.text.strip()
    if not is_youtube_url(url):
        await msg.reply_text("Please send a valid YouTube video, Shorts, or youtu.be URL.")
        return
    if user.id in active_users:
        await msg.reply_text("You already have a download running. Please wait for it to finish.")
        return
    active_users.add(user.id)
    status = await msg.reply_text("🔎 Checking the video and preparing download…")
    quality = context.user_data.get("quality", DEFAULT_QUALITY)
    try:
        with tempfile.TemporaryDirectory(prefix="ytbot-") as tmp:
            file_path = await download_video(url, quality, Path(tmp))
            file_size = file_path.stat().st_size
            if file_size > BOT_API_UPLOAD_LIMIT_BYTES:
                size_mb = file_size / 1_000_000
                await status.edit_text(
                    f"⚠️ This video is {size_mb:.1f} MB, which exceeds Telegram Bot API's 50 MB upload limit. "
                    "The download completed, but Telegram cannot receive it through the hosted Bot API. "
                    "Try /quality 480p or /quality 360p and send the link again."
                )
                return
            await status.edit_text(f"⬆️ Uploading {file_path.name} ({file_size / 1_000_000:.1f} MB)…")
            await context.bot.send_chat_action(chat_id=msg.chat_id, action=ChatAction.UPLOAD_VIDEO)
            with file_path.open("rb") as stream:
                await msg.reply_video(video=stream, caption=f"🎬 {file_path.stem}", read_timeout=180, write_timeout=180,
                                      connect_timeout=30, pool_timeout=30, supports_streaming=True)
            await status.delete()
    except RuntimeError as exc:
        await status.edit_text(f"❌ {exc}")
    except BadRequest as exc:
        error_text = str(exc).lower()
        if any(term in error_text for term in ("file is too big", "request entity too large", "file size exceeded", "too large")):
            try:
                await status.edit_text(
                    "⚠️ Telegram rejected this upload because the file exceeds its Bot API upload limit. "
                    "Try /quality 480p or /quality 360p and send the link again."
                )
            except Exception:
                pass
        else:
            log.exception("Telegram rejected the upload")
            try:
                await status.edit_text(f"❌ Telegram rejected the upload: {exc}")
            except Exception:
                pass
    except Exception:
        log.exception("Unhandled error processing a download")
        try:
            await status.edit_text("❌ Unexpected error. Please try again later.")
        except Exception:
            pass
    finally:
        active_users.discard(user.id)

async def post_init(app: Application) -> None:
    app.bot_data["health_runner"] = web.AppRunner(web.Application())
    health_app = app.bot_data["health_runner"].app
    health_app.router.add_get("/", health)
    health_app.router.add_get("/health", health)
    await app.bot_data["health_runner"].setup()
    site = web.TCPSite(app.bot_data["health_runner"], "0.0.0.0", PORT)
    await site.start()
    log.info("Health endpoint listening on port %s", PORT)

async def post_shutdown(app: Application) -> None:
    runner = app.bot_data.get("health_runner")
    if runner:
        await runner.cleanup()

def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN is required. Set it in your environment or .env file.")
    app = (Application.builder().token(BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("quality", quality_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_link))
    log.info("Starting Telegram bot (python-telegram-bot)")
    app.run_polling(drop_pending_updates=False, allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
