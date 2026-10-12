"""Central configuration for the YouTube Downloader bot.

Every setting has a sensible default and can be overridden with an environment variable of
the same name (see ``.env.example`` for a documented template). Only ``BOT_TOKEN`` is
required — a deploy with nothing else set runs, and says exactly what it is doing.

Importing this module never touches the network or the filesystem, so it is safe to import
from tests and from ``main.py --check``.

The application imposes **no size limits of its own**. The only ceilings that exist are
Telegram's, and they belong to the transport carrying the file:

* the cloud Bot API accepts 50 MB per bot upload,
* a self-hosted ``--local`` Bot API server accepts 2000 MB,
* **MTProto accepts 2000 MB for bots** — which is how this bot sends big files without a
  user account, a phone number or a session string.

Everything else (disk, RAM, bandwidth) is a property of the host, not a policy, and is
reported as such: see :mod:`bot.utils` and the disk guard in :mod:`bot.jobs`.

Sections
--------
1. Telegram
2. Upload transport and Telegram's ceilings
3. Engine (TypeTube)
4. Quality
5. Runtime and housekeeping
6. Web service
7. Logging
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

VERSION = "3.3.3"
PROJECT_NAME = "YouTube Downloader for Telegram"

# --------------------------------------------------------------------------- #
# Telegram's ceilings (not the application's)
#
# These are protocol facts, quoted so the bot can be honest about them and so it can skip
# a transport that physically cannot carry a file. Nothing here is a policy knob: there is
# no setting that lowers them, and none of them is checked before the file exists.
# --------------------------------------------------------------------------- #
#: HTTP Bot API on Telegram's own servers: 50 MB per upload.
BOT_API_UPLOAD_MB = 50
#: A Bot API server running with ``--local``: 2000 MB per upload.
LOCAL_BOT_API_UPLOAD_MB = 2000
#: MTProto (the protocol behind every official client): 2000 MB per upload for bots.
#: 4000 MB exists only for Telegram Premium *user* accounts, which a bot cannot be.
MT_UPLOAD_MB = 2000

#: Kept for callers that think in bytes.
MB = 1024 * 1024

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


#: Telegram accepts exactly this alphabet for the webhook secret token. Render's
#: ``generateValue`` (and plenty of password generators) produce values containing
#: ``+``, ``/`` and ``=``, which makes ``setWebhook`` fail with
#: "Secret token contains illegal characters" — and a failed registration means the bot
#: never receives an update, i.e. it looks offline. Sanitising here turns that outage
#: into a warning.
SECRET_ALPHABET = re.compile(r"[^A-Za-z0-9_-]")
MAX_SECRET_LENGTH = 256


def url_host(url: str) -> str:
    """Host of a URL, lower-cased and without a port — for comparing two of them."""
    text = (url or "").strip()
    if "//" not in text:
        return text.rstrip("/").lower()
    return urlsplit(text).netloc.lower() if urlsplit(text).netloc else text.rstrip("/").lower()


def sanitize_webhook_secret(value: str) -> str:
    """Return ``value`` restricted to Telegram's secret-token alphabet."""
    return SECRET_ALPHABET.sub("", value or "")[:MAX_SECRET_LENGTH]


def normalize_username(value: str) -> str:
    """``@TechnicslSerena`` → ``TechnicslSerena`` (Telegram usernames have no ``@`` in APIs)."""
    return (value or "").strip().lstrip("@").strip()


# --------------------------------------------------------------------------- #
# Settings
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Settings:
    """Immutable snapshot of the configuration."""

    # 1. Telegram -----------------------------------------------------------
    bot_token: str = ""
    #: The owner (diagnostics only — the bot itself is open to everybody).
    owner_user_id: int = 0
    #: ``@username`` of the owner, resolved to an id at startup so nobody has to hunt for
    #: a numeric id.
    owner_username: str = ""
    startup_notify_chat_id: int = 0
    telegram_api_base: str = ""
    #: Telegram restricts the webhook secret token alphabet; see ``sanitize_webhook_secret``.
    webhook_secret_was_fixed: bool = False

    # 2. Upload transport ---------------------------------------------------
    # MTProto is authorised with the *bot token*: api_id/api_hash identify the Telegram
    # application, the token identifies the account. No phone number, no login code, no
    # session string, nothing to store.
    api_id: int = 0
    api_hash: str = ""
    #: Parallel connections per upload. More = faster on a good link (failures are retried
    #: per part); 8 is Telegram's practical maximum and the default.
    mt_connections: int = 8
    upload_transport: str = "auto"  # auto | bot | mtproto

    # 2b. Housekeeping ------------------------------------------------------
    #: 0 = no duration limit. A hosting guard, not a size policy: it refuses a video
    #: before anything is downloaded, because a 10-hour 4K download helps nobody.
    max_duration_minutes: int = 0
    #: Refuse to start a job when the workspace has less than this free. Disk is the
    #: host's constraint, so it is reported on its own terms — never as a file-size cap.
    min_free_disk_mb: int = 256
    #: Seconds a user must wait between two links. Keeps a public bot fair and stops one
    #: person from occupying the whole queue; 0 disables the check.
    user_cooldown_seconds: int = 5
    #: Start image and animated progress message instead of plain text.
    rich_messages: bool = True
    #: Show the operator's credit line on /start. Off by default: a public bot does not
    #: advertise an owner id to strangers, and the owner already knows who they are.
    show_owner_in_start: bool = False
    #: Where Telegram's file_ids for the bot's own images are cached. Defaults to a hidden
    #: file in the download workspace; point it at a mounted volume to survive restarts.
    asset_cache_path: str = ""

    #: Seconds between status-message edits while a job runs. Telegram allows roughly one
    #: edit per second per chat, and the operator asked for a 5 s floor: fewer edits, no
    #: rate limits, still live. The floor that matters is enforced here.
    progress_min_interval: float = 5.0

    # 3. Fetcher (aria2) — torrents and direct links -------------------------
    aria2_path: str = "aria2c"
    #: How long to wait for peers to hand over a magnet link's metadata.
    torrent_metadata_timeout: int = 180
    #: Refuse links that point at the server's own network (loopback, private ranges,
    #: cloud metadata endpoints). A public bot that fetches any URL a stranger sends is a
    #: request-forgery tool; turn this on only for local testing.
    allow_private_links: bool = False

    # 4. Engine (TypeTube) --------------------------------------------------
    typetube_bin: str = "typetube"
    typetube_api_key: str = ""
    typetube_host: str = ""
    #: 0 = auto (chosen from the CPU budget the host actually grants, see bot.utils.cpu_budget).
    typetube_workers: int = 0
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
    #: Where the URL came from: ``WEBHOOK_URL``, ``PUBLIC_URL`` or the platform's own
    #: ``RENDER_EXTERNAL_URL``. Reported so a stale setting is visible in ``/diag``.
    webhook_url_source: str = ""
    #: The URL the hosting platform says the service is published at (Render exports one).
    render_external_url: str = ""
    #: A ``WEBHOOK_URL`` that disagrees with the platform's URL and was therefore ignored.
    stale_webhook_url: str = ""
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
    def bot_api_capability_mb(self) -> int:
        """What the Bot API in use accepts per upload (Telegram's number, not ours)."""
        return LOCAL_BOT_API_UPLOAD_MB if self.uses_local_api else BOT_API_UPLOAD_MB

    @property
    def bot_id(self) -> int:
        """The bot's own numeric id, taken from the token."""
        head = self.bot_token.split(":", 1)[0] if self.bot_token else ""
        return int(head) if head.isdigit() else 0

    @property
    def mtproto_ready(self) -> bool:
        """True when the MTProto transport can start: api_id, api_hash and a bot token.

        No user account is involved. ``api_id``/``api_hash`` identify the Telegram
        *application*; the bot token authorises the bot itself, which is why nothing has to
        be logged into and no session string has to be stored.
        """
        return bool(self.api_id and self.api_hash and self.bot_id)

    @property
    def mtproto_capability_mb(self) -> int:
        """What MTProto accepts per upload for a bot: Telegram's 2000 MB protocol ceiling."""
        return MT_UPLOAD_MB

    @property
    def transport(self) -> str:
        """Which transport carries a file the Bot API cannot: ``mtproto`` or ``bot``."""
        if self.upload_transport == "bot":
            return "bot"
        return "mtproto" if self.mtproto_ready else "bot"

    @property
    def upload_capability_mb(self) -> int:
        """The largest file this deployment can deliver, set by Telegram alone.

        The application adds nothing on top: there is no ``MAX_UPLOAD_MB`` to lower it, and
        the only check anywhere is against the transport that will actually carry the file.
        """
        caps = [self.bot_api_capability_mb]
        if self.transport == "mtproto":
            caps.append(self.mtproto_capability_mb)
        return max(caps)

    @property
    def upload_capability_bytes(self) -> int:
        return self.upload_capability_mb * MB

    @property
    def resolved_asset_cache_path(self) -> Path:
        """The file_id cache: ``ASSET_CACHE_PATH``, else a hidden file in the workspace."""
        return Path(self.asset_cache_path) if self.asset_cache_path else self.download_dir / ".asset-cache.json"

    @property
    def min_free_disk_bytes(self) -> int:
        return self.min_free_disk_mb * MB

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
        raw_secret = _str("WEBHOOK_SECRET")
        # Render exports the address it actually publishes the service at. A WEBHOOK_URL
        # left over from an earlier name (this really happened: the service was renamed and
        # the old host was kept) makes Telegram deliver updates to a host that no longer
        # answers — the bot looks offline while logging perfect health. The platform's own
        # URL is ground truth, so it wins; the operator is told about the disagreement.
        explicit_url = _str("WEBHOOK_URL") or _str("PUBLIC_URL")
        explicit_source = "WEBHOOK_URL" if _str("WEBHOOK_URL") else "PUBLIC_URL"
        platform_url = _str("RENDER_EXTERNAL_URL")
        webhook_url = explicit_url
        webhook_source = explicit_source if explicit_url else ""
        stale_url = ""
        if platform_url and (not explicit_url or url_host(platform_url) != url_host(explicit_url)):
            stale_url = explicit_url
            webhook_url = platform_url
            webhook_source = "RENDER_EXTERNAL_URL"

        secret = sanitize_webhook_secret(raw_secret)
        secret_was_fixed = bool(raw_secret) and secret != raw_secret
        return cls(
            # Telegram
            bot_token=_str("BOT_TOKEN"),
            owner_user_id=_int("OWNER_USER_ID", 0, minimum=0),
            owner_username=normalize_username(_str("OWNER_USERNAME") or _str("OWNER")),
            startup_notify_chat_id=_int("STARTUP_NOTIFY_CHAT_ID", 0, minimum=0),
            telegram_api_base=_str("TELEGRAM_API_BASE").rstrip("/"),
            # Limits
            max_duration_minutes=_int("MAX_DURATION_MINUTES", 0, minimum=0),
            min_free_disk_mb=_int("MIN_FREE_DISK_MB", 256, minimum=0),
            user_cooldown_seconds=_int("USER_COOLDOWN_SECONDS", 5, minimum=0, maximum=3600),
            rich_messages=_bool("RICH_MESSAGES", True),
            show_owner_in_start=_bool("SHOW_OWNER_IN_START", False),
            asset_cache_path=_str("ASSET_CACHE_PATH"),
            # MTProto, authorised with the bot token (both spellings are accepted)
            api_id=_int("TELEGRAM_API_ID", 0, minimum=0) or _int("API_ID", 0, minimum=0),
            api_hash=_str("TELEGRAM_API_HASH") or _str("API_HASH"),
            mt_connections=_int("MT_CONNECTIONS", 8, minimum=1, maximum=8),
            upload_transport=(_str("UPLOAD_TRANSPORT", "auto") or "auto").lower(),
            # Engine
            typetube_bin=_str("TYPETUBE_BIN", "typetube") or "typetube",
            typetube_api_key=_str("TYPETUBE_API_KEY"),
            typetube_host=_str("TYPETUBE_HOST"),
            # 0 = decide from the CPU budget the host actually gives us (see bot.utils.cpu_budget).
            progress_min_interval=_float("PROGRESS_MIN_INTERVAL", 5.0, minimum=0.5),
            aria2_path=_tool("ARIA2_BINARY", "aria2c"),
            torrent_metadata_timeout=_int("TORRENT_METADATA_TIMEOUT", 180, minimum=30),
            allow_private_links=_bool("ALLOW_PRIVATE_LINKS", False),
            typetube_workers=_int("TYPETUBE_WORKERS", 0, minimum=0, maximum=32),
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
            webhook_url=webhook_url,
            webhook_url_source=webhook_source,
            render_external_url=platform_url,
            stale_webhook_url=stale_url,
            webhook_path=_str("WEBHOOK_PATH", "telegram").strip("/") or "telegram",
            webhook_secret=secret,
            webhook_secret_was_fixed=secret_was_fixed,
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

        # A half-configured API key pair is a typo, not a limit: say so instead of failing later.
        if (self.api_id or self.api_hash) and not self.mtproto_ready:
            missing = [
                name
                for name, value in (("TELEGRAM_API_ID", self.api_id), ("TELEGRAM_API_HASH", self.api_hash))
                if not value
            ]
            problems.append(
                "MTProto uploads need both TELEGRAM_API_ID and TELEGRAM_API_HASH from "
                f"https://my.telegram.org — missing: {', '.join(missing)}. (The bot token is the "
                "BOT_TOKEN you already have; no user account or login code is involved.)"
            )

        return problems

    def warnings(self) -> list[str]:
        """Non-fatal advice worth printing at startup."""
        notes: list[str] = []

        if self.transport == "mtproto":
            notes.append(
                "Large uploads use MTProto with your bot token: Telegram allows up to "
                f"{self.mtproto_capability_mb} MB per file that way, and nothing has to be logged into. "
                "The bot token, TELEGRAM_API_ID and TELEGRAM_API_HASH are all that is needed."
            )
        elif self.uses_local_api:
            notes.append(
                f"Uploads go to a local Bot API server, which accepts {self.bot_api_capability_mb} MB per file."
            )
        else:
            notes.append(
                f"Without TELEGRAM_API_ID and TELEGRAM_API_HASH the bot is limited to the Bot API's "
                f"{self.bot_api_capability_mb} MB per file. Adding them costs nothing, needs no account "
                f"login, and raises the ceiling to {self.mtproto_capability_mb} MB — see .env.example \u00a72."
            )

        if self.stale_webhook_url and not self.use_polling:
            notes.append(
                f"WEBHOOK_URL is set to {self.stale_webhook_url}, but this service is published at "
                f"{self.render_external_url}. Using the platform's address so Telegram can reach the "
                "bot; update or delete WEBHOOK_URL to silence this."
            )

        if self.upload_transport == "mtproto" and not self.mtproto_ready:
            notes.append("UPLOAD_TRANSPORT=mtproto was requested but TELEGRAM_API_ID/TELEGRAM_API_HASH are missing.")

        if not self.runs_webhook and not self.use_polling:
            notes.append(
                "WEBHOOK_URL is not set, so the bot uses long polling. A Web Service deployment should set "
                "WEBHOOK_URL to its public https URL."
            )

        if self.webhook_secret_was_fixed:
            notes.append(
                "WEBHOOK_SECRET contained characters Telegram does not accept and was cleaned up to "
                f"\"{self.webhook_secret}\". Telegram only allows A-Z a-z 0-9 _ - (max 256), and registering a "
                "webhook with anything else fails, which would leave the bot looking offline. Set a compliant "
                "value yourself to silence this note."
            )
        if self.runs_webhook and not self.webhook_secret:
            notes.append(
                "WEBHOOK_SECRET is empty: the webhook endpoint accepts anyone who knows its URL. Set it to a "
                "value made of A-Z a-z 0-9 _ - (up to 256 characters)."
            )

        if not self.owner_user_id and not self.owner_username:
            notes.append(
                "No owner configured (OWNER_USERNAME or OWNER_USER_ID): the bot is open to everyone — which is "
                "the default — but nobody can use /diag. Set OWNER_USERNAME to your @username to get "
                "owner-only diagnostics without hunting for a numeric id."
            )

        if not self.typetube_api_key:
            notes.append(
                "No TYPETUBE_API_KEY: using TypeTube public access (per-IP quotas). A private key lifts them and "
                "enables Hi-Fi audio."
            )

        if self.max_concurrent_jobs > 4:
            notes.append(f"MAX_CONCURRENT_JOBS={self.max_concurrent_jobs} downloads at once can exhaust a small VPS.")

        if self.transport == "mtproto" and self.mt_connections > 8:
            notes.append("MT_CONNECTIONS is capped at 8 by Telegram's client limits.")

        if self.typetube_workers == 0:
            notes.append(
                "TYPETUBE_WORKERS is unset: the download worker count is chosen from the CPU budget the host "
                "really gives this container. Set it (1-32) to override."
            )

        if self.min_free_disk_mb < 128:
            notes.append(
                "MIN_FREE_DISK_MB is very low: a job that fills the disk mid-download fails, and the "
                "workspace needs room for the file plus its remux."
            )

        return notes

    def summary(self) -> dict[str, Any]:
        """Non-secret view of the configuration, for logs and ``/status``."""
        return {
            "version": VERSION,
            "api_mode": self.api_mode,
            "api_base": self.api_base,
            "upload_capability_mb": self.upload_capability_mb,
            "bot_api_capability_mb": self.bot_api_capability_mb,
            "mtproto_capability_mb": self.mtproto_capability_mb if self.mtproto_ready else 0,
            "mtproto": "ready (bot token)" if self.mtproto_ready else "off",
            "upload_transport": self.transport,
            "min_free_disk_mb": self.min_free_disk_mb,
            "max_duration_minutes": self.max_duration_minutes or "unlimited",
            "max_concurrent_jobs": self.max_concurrent_jobs,
            "default_quality": self.default_quality,
            "engine": self.typetube_client,
            "typetube_key": "configured" if self.typetube_api_key else "none",
            "typetube_host": self.typetube_host or "cluster default",
            "ffmpeg": self.ffmpeg_path,
            "download_dir": str(self.download_dir),
            "mode": "webhook" if self.runs_webhook else "polling",
            "webhook": self.webhook_target if self.runs_webhook else "",
            "webhook_source": self.webhook_url_source or "-",
            "stale_webhook_url": self.stale_webhook_url,
            "port": self.port,
            "log_level": self.log_level,
            "owner_user_id": self.owner_user_id or "-",
            "owner_username": self.owner_username or "-",
            "public_bot": not (self.owner_user_id or self.owner_username),
            "rich_messages": self.rich_messages,
            "show_owner_in_start": self.show_owner_in_start,
            "user_cooldown_seconds": self.user_cooldown_seconds,
            "mt_connections": self.mt_connections,
            "typetube_workers": self.typetube_workers or "auto",
            "progress_min_interval": self.progress_min_interval,
            "aria2": self.aria2_path,
            "webhook_secret": "set" if self.webhook_secret else "none",
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
    "BOT_API_UPLOAD_MB",
    "LOCAL_BOT_API_UPLOAD_MB",
    "MB",
    "MT_UPLOAD_MB",
    "PROJECT_NAME",
    "SETTINGS",
    "VERSION",
    "ConfigError",
    "Settings",
    "load_settings",
    "require_settings",
]
