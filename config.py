"""Central configuration for the YouTube Downloader bot.

Every setting has a sensible default and can be overridden with an environment variable of
the same name (see ``.env.example`` for a documented template). Only ``BOT_TOKEN`` is
required — a deploy with nothing else set runs, and says exactly what it is doing.

Importing this module never touches the network or the filesystem, so it is safe to import
from tests and from ``main.py --check``.

Sections
--------
1. Telegram
2. Upload limits
3. Engine (TypeTube)
4. Quality
5. Runtime and housekeeping
6. Web service
7. Logging
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VERSION = "3.2.0"
PROJECT_NAME = "YouTube Downloader for Telegram"

#: Telegram cloud Bot API: bots may upload at most 50 MB per file.
CLOUD_UPLOAD_LIMIT_MB = 50
#: A local Bot API server started with ``--local`` raises that ceiling to 2000 MB.
#: This is Telegram's limit for *bots*: Premium's 4 GB applies to user uploads only.
LOCAL_UPLOAD_LIMIT_MB = 2000
#: MTProto *user* sessions (Pyrofork) may upload 2000 MB per file — the route this bot
#: uses for big files, because it needs no extra service and no Premium account.
MT_UPLOAD_LIMIT_MB = 2000
#: …and 4000 MB when that account has Telegram Premium (MT_PREMIUM_ACCOUNT=1).
MT_PREMIUM_UPLOAD_LIMIT_MB = 4000

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
    except ValueError as exc:
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
    except ValueError as exc:
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


def _tool(name: str, default: str) -> str:
    """Resolve a binary from an explicit path, PATH, or a known fallback."""
    explicit = os.getenv(name)
    if explicit:
        return explicit.strip()
    return shutil.which(default) or default


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
    owner_user_id: int = 0
    startup_notify_chat_id: int = 0
    telegram_api_base: str = ""

    # 2. Upload limits ------------------------------------------------------
    max_upload_mb: int = 0
    max_duration_minutes: int = 0

    # 2b. MTProto delivery (Pyrofork user session) --------------------------
    # This is what makes 2 GB files possible without hosting a Bot API server:
    # the bot uploads through a logged-in account and copies the file server-side.
    api_id: int = 0
    api_hash: str = ""
    session_string: str = ""
    storage_chat_id: int = 0
    mt_connections: int = 4
    mt_premium: bool = False
    upload_transport: str = "auto"  # auto | bot | mtproto

    # 3. Engine (TypeTube) --------------------------------------------------
    typetube_bin: str = "typetube"
    typetube_api_key: str = ""
    typetube_host: str = ""
    typetube_workers: int = 8
    typetube_chunk_mb: int = 10
    node_binary: str = "node"

    # 4. Quality ------------------------------------------------------------
    default_quality: str = "720p"

    # 5. Runtime and housekeeping -------------------------------------------
    download_dir: Path = Path("/tmp/ytbot-downloads")
    max_concurrent_jobs: int = 2
    download_timeout_seconds: int = 3600
    progress_interval: float = 2.0
    #: Telegram rejects thumbnails wider than 320 px, so this is also the hard cap.
    thumbnail_max_side: int = 320
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    # 6. Web service --------------------------------------------------------
    webhook_url: str = ""
    webhook_path: str = "telegram"
    webhook_secret: str = ""
    listen_host: str = "0.0.0.0"
    port: int = 8080
    use_polling: bool = False

    # 7. Logging ------------------------------------------------------------
    log_level: str = "INFO"

    # -- derived ------------------------------------------------------------
    @property
    def uses_local_api(self) -> bool:
        """True when requests go to a self-hosted Bot API server."""
        return bool(self.telegram_api_base) and self.telegram_api_base.rstrip("/") != "https://api.telegram.org"

    @property
    def api_upload_cap_mb(self) -> int:
        """Ceiling of the *Bot API* in use (50 MB in the cloud, 2000 MB when self-hosted)."""
        return LOCAL_UPLOAD_LIMIT_MB if self.uses_local_api else CLOUD_UPLOAD_LIMIT_MB

    @property
    def bot_id(self) -> int:
        """The bot's own numeric id, taken from the token (used as the upload target)."""
        head = self.bot_token.split(":", 1)[0] if self.bot_token else ""
        return int(head) if head.isdigit() else 0

    @property
    def mtproto_ready(self) -> bool:
        """True when a usable MTProto session is configured."""
        return bool(self.api_id and self.api_hash and self.session_string and self.bot_id)

    @property
    def mtproto_cap_mb(self) -> int:
        """Per-file ceiling of the user session (Telegram's limit for accounts)."""
        return MT_PREMIUM_UPLOAD_LIMIT_MB if self.mt_premium else MT_UPLOAD_LIMIT_MB

    @property
    def transport(self) -> str:
        """Transport that carries files too big for the Bot API: ``mtproto`` or ``bot``."""
        if self.upload_transport == "bot":
            return "bot"
        return "mtproto" if self.mtproto_ready else "bot"

    @property
    def upload_cap_mb(self) -> int:
        """Hard ceiling of the whole setup: the better of the Bot API and the session."""
        caps = [self.api_upload_cap_mb]
        if self.transport == "mtproto":
            caps.append(self.mtproto_cap_mb)
        return max(caps)

    @property
    def upload_limit_mb(self) -> int:
        """Effective upload ceiling in MB.

        ``MAX_UPLOAD_MB`` is *clamped* rather than rejected: a ceiling this bot cannot honour
        is a reason to warn loudly, not to refuse to boot (that only creates deploy loops).
        """
        if self.max_upload_mb:
            return min(self.max_upload_mb, self.upload_cap_mb)
        return self.upload_cap_mb

    @property
    def upload_limit_bytes(self) -> int:
        return self.upload_limit_mb * 1024 * 1024

    @property
    def api_base(self) -> str:
        return self.telegram_api_base.rstrip("/") or "https://api.telegram.org"

    @property
    def api_mode(self) -> str:
        return "local Bot API server" if self.uses_local_api else "cloud Bot API"

    @property
    def runs_webhook(self) -> bool:
        return bool(self.webhook_url) and not self.use_polling

    @property
    def webhook_target(self) -> str:
        return f"{self.webhook_url.rstrip('/')}/{self.webhook_path.strip('/')}"

    @property
    def notify_chat_id(self) -> int:
        """Where the startup introduction goes: an explicit chat, else the owner."""
        return self.startup_notify_chat_id or self.owner_user_id

    @property
    def typetube_client(self) -> str:
        """How the CLI is described in logs and ``/about``."""
        return "TypeTube" + (" (private key)" if self.typetube_api_key else " (public access)")

    # -- constructors -------------------------------------------------------
    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> Settings:
        """Build settings from ``env`` (defaults to ``os.environ``)."""
        if env is not None:
            previous = os.environ.copy()
            try:
                os.environ.update({key: str(value) for key, value in env.items()})
                return cls.from_env()
            finally:
                os.environ.clear()
                os.environ.update(previous)

        ffmpeg_path = _tool("FFMPEG_BINARY", "ffmpeg")
        return cls(
            # Telegram
            bot_token=_str("BOT_TOKEN"),
            owner_user_id=_int("OWNER_USER_ID", 0, minimum=0),
            startup_notify_chat_id=_int("STARTUP_NOTIFY_CHAT_ID", 0, minimum=0),
            telegram_api_base=_str("TELEGRAM_API_BASE").rstrip("/"),
            # Limits
            max_upload_mb=_int("MAX_UPLOAD_MB", 0, minimum=0),
            max_duration_minutes=_int("MAX_DURATION_MINUTES", 0, minimum=0),
            # MTProto delivery (accepts both the TELEGRAM_*/API_* spellings)
            api_id=_int("TELEGRAM_API_ID", 0, minimum=0) or _int("API_ID", 0, minimum=0),
            api_hash=_str("TELEGRAM_API_HASH") or _str("API_HASH"),
            session_string=_str("TELEGRAM_SESSION_STRING") or _str("SESSION_STRING"),
            storage_chat_id=_int("STORAGE_CHAT_ID", 0),
            mt_connections=_int("MT_CONNECTIONS", 4, minimum=1, maximum=8),
            mt_premium=_bool("MT_PREMIUM_ACCOUNT", False),
            upload_transport=(_str("UPLOAD_TRANSPORT", "auto") or "auto").lower(),
            # Engine
            typetube_bin=_str("TYPETUBE_BIN", "typetube") or "typetube",
            typetube_api_key=_str("TYPETUBE_API_KEY"),
            typetube_host=_str("TYPETUBE_HOST"),
            typetube_workers=_int("TYPETUBE_WORKERS", 8, minimum=1, maximum=32),
            typetube_chunk_mb=_int("TYPETUBE_CHUNK_MB", 10, minimum=1, maximum=64),
            node_binary=_tool("NODE_BINARY", "node"),
            # Quality
            default_quality=_str("DEFAULT_QUALITY", "720p") or "720p",
            # Runtime
            download_dir=Path(_str("DOWNLOAD_DIR", "/tmp/ytbot-downloads")).expanduser(),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2, minimum=1, maximum=16),
            download_timeout_seconds=_int("DOWNLOAD_TIMEOUT_SECONDS", 3600, minimum=60),
            progress_interval=_float("PROGRESS_INTERVAL", 2.0, minimum=0.5),
            thumbnail_max_side=_int("THUMBNAIL_MAX_SIDE", 320, minimum=64, maximum=320),
            ffmpeg_path=ffmpeg_path,
            ffprobe_path=_resolve_ffprobe(ffmpeg_path),
            # Web service
            webhook_url=_str("WEBHOOK_URL") or _str("PUBLIC_URL") or _str("RENDER_EXTERNAL_URL"),
            webhook_path=_str("WEBHOOK_PATH", "telegram").strip("/") or "telegram",
            webhook_secret=_str("WEBHOOK_SECRET"),
            listen_host=_str("LISTEN_HOST", "0.0.0.0"),
            port=_int("PORT", 8080, minimum=1, maximum=65535),
            use_polling=_bool("USE_POLLING", False),
            # Logging
            log_level=_str("LOG_LEVEL", "INFO").upper(),
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

        if self.log_level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
            problems.append("LOG_LEVEL must be one of DEBUG, INFO, WARNING, ERROR, CRITICAL")

        if self.upload_transport not in ("auto", "bot", "mtproto"):
            problems.append("UPLOAD_TRANSPORT must be one of auto, bot, mtproto")

        # A half-configured session is a typo, not a limit: say so instead of failing later.
        if (self.api_id or self.api_hash or self.session_string) and not self.mtproto_ready:
            missing = [
                name
                for name, value in (
                    ("TELEGRAM_API_ID", self.api_id),
                    ("TELEGRAM_API_HASH", self.api_hash),
                    ("TELEGRAM_SESSION_STRING", self.session_string),
                )
                if not value
            ]
            problems.append(
                "MTProto uploads need all three of TELEGRAM_API_ID, TELEGRAM_API_HASH and "
                f"TELEGRAM_SESSION_STRING — missing: {', '.join(missing)} "
                "(generate the session with `python scripts/make_session.py`)"
            )

        return problems

    def warnings(self) -> list[str]:
        """Non-fatal advice worth printing at startup."""
        notes: list[str] = []

        if self.max_upload_mb and self.max_upload_mb > self.upload_cap_mb:
            notes.append(
                f"MAX_UPLOAD_MB={self.max_upload_mb} is above what this setup can send, so it is clamped to "
                f"{self.upload_cap_mb} MB — nothing above that would upload successfully."
            )

        if self.transport == "mtproto":
            notes.append(
                f"Large uploads go through a Telegram user session (MTProto): up to {self.mtproto_cap_mb} MB "
                f"per file{' (Premium account)' if self.mt_premium else ''}."
            )
        elif self.api_upload_cap_mb > CLOUD_UPLOAD_LIMIT_MB:
            notes.append(f"Uploads are sent to a local Bot API server, which allows {self.api_upload_cap_mb} MB.")
        else:
            notes.append(
                f"Uploads are limited to {self.api_upload_cap_mb} MB by the cloud Bot API. For bigger files add "
                "TELEGRAM_API_ID + TELEGRAM_API_HASH + TELEGRAM_SESSION_STRING (see scripts/make_session.py, "
                f"2 GB per file), or point TELEGRAM_API_BASE at a local Bot API server."
            )

        if self.upload_transport == "mtproto" and not self.mtproto_ready:
            notes.append("UPLOAD_TRANSPORT=mtproto was requested but the session is incomplete; using the Bot API.")

        if self.mtproto_ready and self.storage_chat_id:
            notes.append(f"MTProto uploads are stored in chat {self.storage_chat_id} (the bot must be a member).")

        if self.mt_premium and self.mtproto_ready and not self.uses_local_api:
            notes.append(
                f"MT_PREMIUM_ACCOUNT=1: the {MT_PREMIUM_UPLOAD_LIMIT_MB} MB ceiling applies only if the session's "
                "account really has Premium (scripts/make_session.py confirms it)."
            )

        if not self.runs_webhook and not self.use_polling:
            notes.append(
                "WEBHOOK_URL is not set, so the bot uses long polling. A Web Service deployment should set "
                "WEBHOOK_URL to its public https URL."
            )

        if not self.owner_user_id:
            notes.append(
                "OWNER_USER_ID is not set: anyone who finds this bot can use it. Set it (send /id to the bot) to "
                "keep the bot to yourself."
            )

        if not self.typetube_api_key:
            notes.append(
                "No TYPETUBE_API_KEY: using TypeTube public access (per-IP quotas). A private key lifts them and "
                "enables Hi-Fi audio."
            )

        if self.max_concurrent_jobs > 4:
            notes.append(f"MAX_CONCURRENT_JOBS={self.max_concurrent_jobs} downloads at once can exhaust a small VPS.")

        if self.uses_local_api and self.max_concurrent_jobs > 1:
            notes.append(
                "With the local Bot API server, each concurrent upload uses about as much disk as the file itself."
            )

        return notes

    def summary(self) -> dict[str, Any]:
        """Non-secret view of the configuration, for logs and ``/status``."""
        return {
            "version": VERSION,
            "api_mode": self.api_mode,
            "api_base": self.api_base,
            "upload_limit_mb": self.upload_limit_mb,
            "upload_cap_mb": self.upload_cap_mb,
            "api_upload_cap_mb": self.api_upload_cap_mb,
            "upload_transport": self.transport,
            "mtproto": "ready" if self.mtproto_ready else "off",
            "mtproto_cap_mb": self.mtproto_cap_mb if self.mtproto_ready else 0,
            "max_duration_minutes": self.max_duration_minutes or "unlimited",
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "default_quality": self.default_quality,
            "engine": self.typetube_client,
            "typetube_key": "configured" if self.typetube_api_key else "none",
            "typetube_host": self.typetube_host or "cluster default",
            "typetube_workers": self.typetube_workers,
            "ffmpeg": self.ffmpeg_path,
            "download_dir": str(self.download_dir),
            "mode": "webhook" if self.runs_webhook else "polling",
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
    "MT_PREMIUM_UPLOAD_LIMIT_MB",
    "MT_UPLOAD_LIMIT_MB",
    "PROJECT_NAME",
    "SETTINGS",
    "VERSION",
    "ConfigError",
    "Settings",
    "load_settings",
    "require_settings",
]
