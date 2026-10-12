"""Link inspection: ask yt-dlp what a URL offers, without downloading it."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from dataclasses import dataclass, field

from bot.errors import NoFormatsError, ProbeError
from bot.qualities import QualityOption, available_heights, build_options, sort_options
from config import Settings

log = logging.getLogger("bot.probe")

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_NOISE_RE = re.compile(r"^(ERROR|WARNING):\s*", re.IGNORECASE)


@dataclass
class MediaInfo:
    """Everything the UI needs to describe a link and its options."""

    url: str
    title: str
    uploader: str
    duration: float
    thumbnail_url: str
    extractor: str
    webpage_url: str
    is_live: bool
    max_height: int | None
    options: list[QualityOption] = field(default_factory=list)
    heights: list[int] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False)

    def option(self, key: str) -> QualityOption | None:
        for option in self.options:
            if option.key == key:
                return option
        return None


def clean_error(message: str) -> str:
    """Turn a yt-dlp error blob into one readable line."""
    text = _ANSI_RE.sub("", message or "")
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return "unknown error"
    first = _NOISE_RE.sub("", lines[0])
    # yt-dlp often appends "Please report this issue on ..." noise.
    first = first.split("; please report this issue")[0]
    return first[:280]


def _build_params(settings: Settings) -> dict:
    params = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": not settings.allow_playlist,
        "nocheckcertificate": False,
        "no_color": True,
        "socket_timeout": settings.ytdlp_socket_timeout,
        "retries": settings.ytdlp_retries,
        "extractor_retries": 2,
        "ignoreerrors": False,
        "extract_flat": False,
        "writethumbnail": False,
        "prefer_free_formats": settings.prefer_free_formats,
    }
    if settings.allow_playlist:
        params["playlist_items"] = "1"
    if settings.cookies_file:
        params["cookiefile"] = settings.cookies_file
    if settings.proxy:
        params["proxy"] = settings.proxy
    if settings.user_agent:
        params["http_headers"] = {"User-Agent": settings.user_agent}
    return params


def _first_entry(info: dict) -> dict:
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        if entries:
            return entries[0]
    return info


def _needs_inspection(info: dict) -> bool:
    """True when yt-dlp gave us a file but no geometry (direct media URLs)."""
    formats = info.get("formats") or []
    if not formats:
        return False
    if any(f.get("height") for f in formats):
        return False
    return bool(info.get("direct")) or len(formats) == 1


async def inspect_direct_url(url: str, settings: Settings) -> dict | None:
    """Ask ffprobe about a direct media URL and turn the answer into a format entry."""
    from bot.process import run_command

    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        url,
    ]
    try:
        result = await run_command(command, timeout=60, label="ffprobe (direct link)")
    except Exception as exc:
        log.debug("ffprobe could not read %s: %s", url, exc)
        return None

    if not result.ok:
        log.debug("ffprobe rejected the direct link %s: %s", url, result.stderr.strip()[:150])
        return None

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError:
        return None

    streams = payload.get("streams") or []
    container = payload.get("format") or {}
    video = next((stream for stream in streams if stream.get("codec_type") == "video"), None)
    audio = next((stream for stream in streams if stream.get("codec_type") == "audio"), None)
    if video is None and audio is None:
        return None

    def _number(value, cast, default=0):
        try:
            return cast(value)
        except (TypeError, ValueError):
            return default

    size = _number(container.get("size"), int)
    duration = _number(container.get("duration"), float)
    container_name = str(container.get("format_name") or "mp4").split(",")[0]
    return {
        "ext": "mp4" if "mp4" in container_name else container_name,
        "vcodec": str((video or {}).get("codec_name") or "none") if video else "none",
        "acodec": str((audio or {}).get("codec_name") or "none") if audio else "none",
        "height": _number((video or {}).get("height"), int) or None,
        "width": _number((video or {}).get("width"), int) or None,
        "filesize": size or None,
        "duration": duration or None,
    }


async def _augment_direct(info: dict, url: str, settings: Settings) -> dict:
    """Give a direct link real geometry so the quality picker can describe it."""
    details = await inspect_direct_url(url, settings)
    if not details:
        return info

    entries = info.get("formats") or [{}]
    for entry in entries:
        entry.setdefault("format_id", "direct")
        entry.update(
            {
                "ext": details["ext"],
                "vcodec": details["vcodec"],
                "acodec": details["acodec"],
                "height": details["height"],
                "width": details["width"],
                "filesize": details["filesize"] or entry.get("filesize"),
            }
        )

    merged = dict(info)
    merged["formats"] = entries
    if not merged.get("duration") and details.get("duration"):
        merged["duration"] = details["duration"]
    if not merged.get("title"):
        merged["title"] = str(merged.get("webpage_url_basename") or "media")
    log.info(
        "Direct link inspected: %sx%s, %s%s",
        details.get("width") or "?",
        details.get("height") or "?",
        f"{details['duration']:.0f}s" if details.get("duration") else "unknown duration",
        ", audio" if details["acodec"] != "none" else ", no audio",
    )
    return merged


def _pick_thumbnail(info: dict) -> str:
    thumbnails = info.get("thumbnails") or []
    if thumbnails:
        ordered = sorted(thumbnails, key=lambda item: (item.get("width") or 0, item.get("height") or 0), reverse=True)
        for item in ordered:
            if item.get("url"):
                return str(item["url"])
    return str(info.get("thumbnail") or "")


def extract_info_sync(url: str, settings: Settings) -> dict:
    """Blocking yt-dlp metadata extraction (runs in a worker thread)."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    try:
        with YoutubeDL(_build_params(settings)) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        raise ProbeError(clean_error(str(exc))) from exc
    except Exception as exc:
        raise ProbeError(clean_error(str(exc))) from exc

    if not info:
        raise ProbeError("The site returned no information for that link.")
    return _first_entry(info)


async def probe(url: str, settings: Settings) -> MediaInfo:
    """Inspect ``url`` and build the quality options it supports."""
    info = await asyncio.to_thread(extract_info_sync, url, settings)

    if _needs_inspection(info):
        info = await _augment_direct(info, url, settings)

    if info.get("is_live"):
        raise ProbeError("Live streams are not supported — the file would never finish.")

    options = sort_options(build_options(info, settings))
    if not options:
        raise NoFormatsError()

    duration = 0.0
    try:
        duration = float(info.get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0

    if settings.max_duration_minutes and duration and duration > settings.max_duration_minutes * 60:
        raise ProbeError(
            f"That video is {duration / 60:.0f} minutes long; this bot is limited to "
            f"{settings.max_duration_minutes} minutes."
        )

    heights = available_heights(info)
    result = MediaInfo(
        url=url,
        title=str(info.get("title") or "Untitled"),
        uploader=str(info.get("uploader") or info.get("channel") or ""),
        duration=duration,
        thumbnail_url=_pick_thumbnail(info),
        extractor=str(info.get("extractor_key") or info.get("extractor") or ""),
        webpage_url=str(info.get("webpage_url") or url),
        is_live=bool(info.get("is_live")),
        max_height=max(heights) if heights else None,
        options=options,
        heights=heights,
        raw=info,
    )
    log.info(
        "Probed %s: %s (%s options, max %sp)",
        result.extractor or "link",
        result.title[:60],
        len(options),
        result.max_height or "audio-only",
    )
    return result


class ProbeCache:
    """Remembers probe results so a button press does not re-inspect the link.

    Keyed by a short token that travels in ``callback_data``; entries expire so a
    stale button can never start the wrong download.
    """

    def __init__(self, ttl_seconds: float = 3600, max_entries: int = 200) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, tuple[MediaInfo, float, int]] = {}

    def put(self, info: MediaInfo, *, user_id: int = 0) -> str:
        self._evict()
        token = uuid.uuid4().hex[:8]
        self._entries[token] = (info, time.monotonic(), user_id)
        return token

    def get(self, token: str, *, user_id: int | None = None) -> MediaInfo | None:
        entry = self._entries.get(token)
        if not entry:
            return None
        info, created, owner = entry
        if time.monotonic() - created > self.ttl:
            self._entries.pop(token, None)
            return None
        if user_id is not None and owner and owner != user_id:
            return None
        return info

    def drop(self, token: str) -> None:
        self._entries.pop(token, None)

    def _evict(self) -> None:
        now = time.monotonic()
        for token, (_, created, _) in list(self._entries.items()):
            if now - created > self.ttl:
                self._entries.pop(token, None)
        while len(self._entries) >= self.max_entries:
            oldest = min(self._entries.items(), key=lambda item: item[1][1])[0]
            self._entries.pop(oldest, None)

    def __len__(self) -> int:
        return len(self._entries)
