"""Configuration for the Telegram downloader. Environment variables override .env values."""
import os
from dotenv import load_dotenv

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "48"))
DEFAULT_QUALITY = os.getenv("DEFAULT_QUALITY", "720p").lower()
ALLOWED_QUALITIES = {"360p", "480p", "720p", "1080p", "1440p", "2160p"}
PORT = int(os.getenv("PORT", "10000"))
DOWNLOAD_TIMEOUT = int(os.getenv("DOWNLOAD_TIMEOUT", "900"))
LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

if MAX_FILE_MB < 1:
    raise ValueError("MAX_FILE_MB must be a positive integer.")
if DEFAULT_QUALITY not in ALLOWED_QUALITIES:
    raise ValueError(f"DEFAULT_QUALITY must be one of: {', '.join(sorted(ALLOWED_QUALITIES))}")
if DOWNLOAD_TIMEOUT < 30:
    raise ValueError("DOWNLOAD_TIMEOUT must be at least 30 seconds.")
