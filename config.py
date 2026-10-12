"""Central configuration for the YouTube Downloader bot.

Every setting has a sensible default and can be overridden with an environment
variable of the same name (see ``.env.example`` for a documented template).
Importing this module never touches the network or the filesystem, so it is safe
to import from tests.

Sections
--------
1. Telegram
2. Upload limits
3. Downloads (yt-dlp)
4. Quality and formats
5. Uploads to Telegram
6. Runtime and housekeeping
7. Web service
8. Logging
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

VERSION = "3.0.0"
PROJECT_NAME = "YouTube Downloader for Telegram"

#: Telegram cloud Bot API: bots may upload at most 50 MB per file.
CLOUD_UPLOAD_LIMIT_MB = 50
#: A local Bot API server started with ``--local`` raises that ceiling to 2000 MB.
LOCAL_UPLOAD_LIMIT_MB = 2000

DEFAULT_UPLOAD_LIMIT_MB = {False: CLOUD_UPLOAD_LIMIT_MB, True: LOCAL_UPLOAD_LIMIT_MB}

_FLAG_VALUES = {
    "1": True,
    "true": True,
    "yes": True,
    "on": True,
    "0": False,
    "false": False,
    "no": False,
    "off": False,
    "": False,
}


class ConfigError(Exception):
    """Raised when the environment cannot produce a usable configuration."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {line}" for line in problems))


# --------------------------------------------------------------------------- #
# Environment helpers
# --------------------------------------------------------------------------- #
def _str(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return default if value is None else value.strip()


def _int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError as exc:  # pragma: no cover - exercised through Settings.validate
        raise ConfigError([f"{name} must be an integer, got {raw!r}"]) from exc
    if minimum is not None and value < minimum:
        raise ConfigError([f"{name} must be >= {minimum}, got {value}"])
    if maximum is not None and value > maximum:
        raise ConfigError([f"{name} must be <= {maximum}, got {value}"])
    return value


def _float(name: str, default: float, *, minimum: float | None = None) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        value = float(str(raw).strip())
    except ValueError as exc:  # pragma: no cover
        raise ConfigError([f"{name} must be a number, got {raw!r}"]) from exc
    if minimum is not None and value < minimum:
        raise ConfigError([f"{name} must be >= {minimum}, got {value}"])
    return value


def _bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    value = _FLAG_VALUES.get(str(raw).strip().lower())
    return default if value is None else value


def _list(name: str, default: tuple[str, ...]) -> tuple[str, ...]:
    raw = os.getenv(name)
    if not raw:
        return default
    items = tuple(part.strip().lower() for part in raw.split(",") if part.strip())
    return items or default


def _executable(name: str, default: str) -> str:
    """Resolve a binary from an explicit path, PATH, or a known fallback."""
    explicit = os.getenv(name)
    if explicit:
        return explicit.strip()
    found = shutil.which(default)
    return found or default


def _resolve_ffprobe(ffmpeg_path: str) -> str:
    """ffprobe normally sits next to ffmpeg; prefer an explicit override."""
    explicit = os.getenv("FFPROBE_BINARY")
    if explicit:
        return explicit.strip()
    sibling = Path(ffmpeg_path)
    if sibling.parent != Path(".") or os.sep in ffmpeg_path:
        candidate = sibling.with_name("ffprobe" + (sibling.suffix if sibling.suffix else ""))
        if candidate.is_file():
            return str(candidate)
    return shutil.which("ffprobe") or "ffprobe"


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the configuration."""

    # 1. Telegram -----------------------------------------------------------
    bot_token: str = ""
    telegram_api_base: str = ""
    owner_user_id: int = 0

    # 2. Upload limits ------------------------------------------------------
    max_upload_mb: int = 0
    max_duration_minutes: int = 0

    # 3. Downloads (yt-dlp) -------------------------------------------------
    download_dir: Path = Path("/tmp/ytbot-downloads")
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    cookies_file: str = ""
    proxy: str = ""
    ytdlp_socket_timeout: int = 30
    ytdlp_retries: int = 10
    fragment_concurrency: int = 4
    download_timeout_seconds: int = 3600
    prefer_free_formats: bool = True
    allow_playlist: bool = False
    user_agent: str = ""
    #: Testing only: permits links that resolve to private/loopback addresses.
    #: Needed by scripts/e2e_local.py, which serves its test file on 127.0.0.1.
    allow_private_urls: bool = False

    # 4. Quality and formats ------------------------------------------------
    quality_ladder: tuple[int, ...] = (144, 240, 360, 480, 720, 1080, 1440, 2160)
    default_quality: str = "720p"
    audio_format: str = "m4a"
    offer_audio_only: bool = True

    # 5. Uploads to Telegram ------------------------------------------------
    progress_interval: float = 2.0
    upload_chunk_size: int = 1024 * 1024
    thumbnail_max_side: int = 320

    # 6. Runtime and housekeeping ------------------------------------------
    max_concurrent_jobs: int = 2
    cleanup_after_upload: bool = True
    keep_failed_files: bool = False
    startup_notify_chat_id: int = 0

    # 7. Web service --------------------------------------------------------
    webhook_url: str = ""
    webhook_path: str = "telegram"
    webhook_secret: str = ""
    listen_host: str = "0.0.0.0"
    port: int = 8080
    use_polling: bool = False

    # 8. Logging ------------------------------------------------------------
    log_level: str = "INFO"
    log_pretty: bool = True

    pipeline: tuple[str, ...] = field(default_factory=tuple)

    # -- derived ------------------------------------------------------------
    @property
    def uses_local_api(self) -> bool:
        """True when requests go to a self-hosted Bot API server."""
        return bool(self.telegram_api_base) and self.telegram_api_base.rstrip("/") != "https://api.telegram.org"

    @property
    def upload_limit_mb(self) -> int:
        """Effective upload ceiling in MB (worker-enforced, mirrors Telegram's)."""
        if self.max_upload_mb:
            return self.max_upload_mb
        return LOCAL_UPLOAD_LIMIT_MB if self.uses_local_api else CLOUD_UPLOAD_LIMIT_MB

    @property
    def upload_limit_bytes(self) -> int:
        return self.upload_limit_mb * 1024 * 1024

    @property
    def api_base(self) -> str:
        return self.telegram_api_base.rstrip("/") or "https://api.telegram.org"

    @property
    def runs_webhook(self) -> bool:
        return bool(self.webhook_url) and not self.use_polling

    @property
    def webhook_target(self) -> str:
        return f"{self.webhook_url.rstrip('/')}/{self.webhook_path.strip('/')}"

    @property
    def api_mode(self) -> str:
        return "local Bot API server" if self.uses_local_api else "cloud Bot API"

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (defaults to ``os.environ``)."""
        if env is not None:
            # Temporarily apply the mapping so the helpers above can be reused.
            previous = os.environ.copy()
            try:
                os.environ.update({k: str(v) for k, v in env.items()})
                return cls.from_env()
            finally:
                os.environ.clear()
                os.environ.update(previous)

        download_dir = Path(_str("DOWNLOAD_DIR", "/tmp/ytbot-downloads")).expanduser()
        ffmpeg_path = _executable("FFMPEG_BINARY", "ffmpeg")
        ffprobe_path = _resolve_ffprobe(ffmpeg_path)

        return cls(
            bot_token=_str("BOT_TOKEN"),
            telegram_api_base=_str("TELEGRAM_API_BASE").rstrip("/"),
            owner_user_id=_int("OWNER_USER_ID", 0, minimum=0),
            max_upload_mb=_int("MAX_UPLOAD_MB", 0, minimum=0, maximum=2000),
            max_duration_minutes=_int("MAX_DURATION_MINUTES", 0, minimum=0),
            download_dir=download_dir,
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=ffprobe_path,
            cookies_file=_str("COOKIES_FILE"),
            proxy=_str("PROXY"),
            ytdlp_socket_timeout=_int("YTDLP_SOCKET_TIMEOUT", 30, minimum=5),
            ytdlp_retries=_int("YTDLP_RETRIES", 10, minimum=0),
            fragment_concurrency=_int("FRAGMENT_CONCURRENCY", 4, minimum=1, maximum=32),
            download_timeout_seconds=_int("DOWNLOAD_TIMEOUT_SECONDS", 3600, minimum=60),
            prefer_free_formats=_bool("PREFER_FREE_FORMATS", True),
            allow_playlist=_bool("ALLOW_PLAYLIST", False),
            user_agent=_str("USER_AGENT"),
            allow_private_urls=_bool("ALLOW_PRIVATE_URLS", False),
            quality_ladder=tuple(
                int(part)
                for part in _list("QUALITY_LADDER", ("144", "240", "360", "480", "720", "1080", "1440", "2160"))
            ),
            default_quality=_str("DEFAULT_QUALITY", "720p"),
            audio_format=_str("AUDIO_FORMAT", "m4a").lower(),
            offer_audio_only=_bool("OFFER_AUDIO_ONLY", True),
            progress_interval=_float("PROGRESS_INTERVAL", 2.0, minimum=0.5),
            upload_chunk_size=_int("UPLOAD_CHUNK_SIZE", 1024 * 1024, minimum=64 * 1024),
            thumbnail_max_side=_int("THUMBNAIL_MAX_SIDE", 320, minimum=64, maximum=320),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2, minimum=1, maximum=16),
            cleanup_after_upload=_bool("CLEANUP_AFTER_UPLOAD", True),
            keep_failed_files=_bool("KEEP_FAILED_FILES", False),
            startup_notify_chat_id=_int("STARTUP_NOTIFY_CHAT_ID", 0),
            webhook_url=_str("WEBHOOK_URL") or _str("PUBLIC_URL") or _str("RENDER_EXTERNAL_URL"),
            webhook_path=_str("WEBHOOK_PATH", "telegram").strip("/") or "telegram",
            webhook_secret=_str("WEBHOOK_SECRET"),
            listen_host=_str("LISTEN_HOST", "0.0.0.0"),
            port=_int("PORT", 8080, minimum=1, maximum=65535),
            use_polling=_bool("USE_POLLING", False),
            log_level=_str("LOG_LEVEL", "INFO").upper(),
            log_pretty=_bool("LOG_PRETTY", True),
        )

    # -- validation ---------------------------------------------------------
    def validate(self) -> list[str]:
        """Return a list of human-readable problems (empty when usable)."""
        problems: list[str] = []

        if not self.bot_token:
            problems.append("BOT_TOKEN is required (get it from @BotFather)")
        elif ":" not in self.bot_token or len(self.bot_token.split(":", 1)[1]) < 20:
            problems.append("BOT_TOKEN does not look like a BotFather token (expected 123456789:AA...)")

        if self.telegram_api_base and not self.telegram_api_base.startswith(("http://", "https://")):
            problems.append("TELEGRAM_API_BASE must start with http:// or https://")

        if self.webhook_url and not self.webhook_url.startswith(("http://", "https://")):
            problems.append("WEBHOOK_URL must start with http:// or https://")

        if not self.runs_webhook and not self.use_polling and self.webhook_url == "":
            pass  # polling is a valid fallback and is reported, not an error

        for height in self.quality_ladder:
            if height < 144 or height > 4320:
                problems.append(f"QUALITY_LADDER contains an implausible height: {height}")

        if self.audio_format not in ("m4a", "mp3"):
            problems.append("AUDIO_FORMAT must be m4a or mp3")

        if self.max_upload_mb and self.max_upload_mb > self.api_upload_cap_mb:
            problems.append(
                f"MAX_UPLOAD_MB={self.max_upload_mb} exceeds what {self.api_mode} accepts "
                f"({self.api_upload_cap_mb} MB) — every upload above that would fail"
            )

        if self.cookies_file and not Path(self.cookies_file).is_file():
            problems.append(f"COOKIES_FILE does not exist: {self.cookies_file}")

        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            problems.append("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

        return problems

    @property
    def api_upload_cap_mb(self) -> int:
        """Hard ceiling of the Bot API in use (Telegram's own limit)."""
        return LOCAL_UPLOAD_LIMIT_MB if self.uses_local_api else CLOUD_UPLOAD_LIMIT_MB

    def warnings(self) -> list[str]:
        """Non-fatal advice worth printing at startup."""
        notes: list[str] = []

        if not self.uses_local_api:
            notes.append(
                f"Using the cloud Bot API: uploads are capped at {CLOUD_UPLOAD_LIMIT_MB} MB by Telegram. "
                "Point TELEGRAM_API_BASE at a local Bot API server (--local) to reach "
                f"{LOCAL_UPLOAD_LIMIT_MB} MB — see docs/DEPLOYMENT.md."
            )
        elif self.upload_limit_mb < LOCAL_UPLOAD_LIMIT_MB:
            notes.append(
                f"MAX_UPLOAD_MB={self.upload_limit_mb} is lower than the {LOCAL_UPLOAD_LIMIT_MB} MB your "
                "local Bot API server allows."
            )

        if not self.runs_webhook and not self.use_polling:
            notes.append(
                "WEBHOOK_URL is not set, so the bot falls back to long polling. A Web Service "
                "deployment should set WEBHOOK_URL to its public https URL."
            )

        if self.cleanup_after_upload is False:
            notes.append("CLEANUP_AFTER_UPLOAD is off: finished downloads stay on disk until the next restart.")

        if self.allow_private_urls:
            notes.append(
                "ALLOW_PRIVATE_URLS is enabled: links may point at private network addresses. "
                "Use it for local testing only."
            )

        if self.max_concurrent_jobs > 4:
            notes.append(
                f"MAX_CONCURRENT_JOBS={self.max_concurrent_jobs} downloads at once can exhaust a small instance."
            )

        return notes

    def summary(self) -> dict[str, Any]:
        """Non-secret view of the configuration, for logs and ``/status``."""
        return {
            "version": VERSION,
            "api_mode": self.api_mode,
            "api_base": self.api_base,
            "upload_limit_mb": self.upload_limit_mb,
            "api_upload_cap_mb": self.api_upload_cap_mb,
            "max_duration_minutes": self.max_duration_minutes or "unlimited",
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "default_quality": self.default_quality,
            "quality_ladder": list(self.quality_ladder),
            "audio_format": self.audio_format,
            "ffmpeg": self.ffmpeg_path,
            "ffprobe": self.ffprobe_path,
            "cookies": "configured" if self.cookies_file else "none",
            "allow_private_urls": self.allow_private_urls,
            "proxy": "configured" if self.proxy else "none",
            "download_dir": str(self.download_dir),
            "mode": "webhook" if self.runs_webhook else "polling",
            "webhook_url": self.webhook_url or "-",
            "port": self.port,
            "log_level": self.log_level,
            "owner_user_id": self.owner_user_id or "-",
        }


#: Module-level settings, imported by the rest of the application.
SETTINGS = Settings.from_env()


def load_settings(env: dict[str, str] | None = None) -> Settings:
    """Load settings from ``env`` (defaults to the process environment)."""
    return Settings.from_env(env)


def require_settings(env: dict[str, str] | None = None) -> Settings:
    """Load settings and raise :class:`ConfigError` when they are unusable."""
    settings = Settings.from_env(env)
    problems = settings.validate()
    if problems:
        raise ConfigError(problems)
    return settings


__all__ = [
    "CLOUD_UPLOAD_LIMIT_MB",
    "LOCAL_UPLOAD_LIMIT_MB",
    "PROJECT_NAME",
    "SETTINGS",
    "VERSION",
    "ConfigError",
    "Settings",
    "load_settings",
    "require_settings",
]
