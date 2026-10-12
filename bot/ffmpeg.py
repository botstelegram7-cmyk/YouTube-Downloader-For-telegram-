"""FFmpeg and ffprobe helpers: metadata, thumbnails, faststart, audio conversion.

Everything Telegram needs from a media file is decided here:

* real width, height and duration (so the video is not a generic document),
* a JPEG thumbnail under Telegram's 200 kB / 320 px limits,
* the ``moov`` atom at the front of the file, so clients can start playing before
  the whole file is downloaded.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from bot.errors import CommandError, MissingDependency
from bot.process import ProcessRegistry, run_command
from config import Settings

log = logging.getLogger("bot.ffmpeg")

THUMBNAIL_LIMIT_BYTES = 200 * 1024  # Telegram rejects larger thumbnails
FASTSTART_SCAN_BYTES = 4 * 1024 * 1024


@dataclass
class MediaProbe:
    """What ffprobe reports about a file."""

    path: Path
    duration: float
    width: int
    height: int
    size: int
    has_video: bool
    has_audio: bool
    video_codec: str
    audio_codec: str
    container: str

    @property
    def resolution(self) -> str:
        if not self.has_video or not self.width or not self.height:
            return ""
        if self.height >= 2160:
            return f"{self.width}×{self.height} (4K)"
        if self.height >= 1440:
            return f"{self.width}×{self.height} (QHD)"
        if self.height >= 1080:
            return f"{self.width}×{self.height} (FHD)"
        if self.height >= 720:
            return f"{self.width}×{self.height} (HD)"
        return f"{self.width}×{self.height}"


async def ffmpeg_version(settings: Settings) -> str | None:
    """``"7.0.2"`` or ``None`` when FFmpeg is not installed."""
    try:
        result = await run_command([settings.ffmpeg_path, "-version"], timeout=15, label="ffmpeg -version")
    except (FileNotFoundError, PermissionError, CommandError, OSError):
        return None
    if not result.ok:
        return None
    first = (result.stdout or result.stderr).splitlines()[0] if (result.stdout or result.stderr) else ""
    parts = first.split()
    return parts[2] if len(parts) > 2 else first or None


async def ensure_available(settings: Settings) -> None:
    """Raise a clear error when the media toolchain is missing."""
    if await ffmpeg_version(settings) is None:
        raise MissingDependency(
            f"FFmpeg was not found at '{settings.ffmpeg_path}'. Install it (apt-get install ffmpeg) "
            "or set FFMPEG_BINARY to its path."
        )


async def probe_media(
    path: Path,
    settings: Settings,
    *,
    registry: ProcessRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
) -> MediaProbe:
    """Read width, height, duration and codecs from a media file."""
    command = [
        settings.ffprobe_path,
        "-v",
        "error",
        "-print_format",
        "json",
        "-show_format",
        "-show_streams",
        str(path),
    ]
    try:
        result = await run_command(command, timeout=60, registry=registry, cancel_event=cancel_event, label="ffprobe")
    except FileNotFoundError as exc:
        raise MissingDependency(
            f"ffprobe was not found at '{settings.ffprobe_path}'. It ships with FFmpeg; "
            "set FFPROBE_BINARY if it lives elsewhere."
        ) from exc

    if not result.ok:
        raise CommandError("ffprobe", result.stderr.strip()[:300])

    try:
        payload = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise CommandError("ffprobe", "unreadable JSON output") from exc

    streams = payload.get("streams") or []
    container = payload.get("format") or {}

    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)

    duration = 0.0
    for candidate in (container.get("duration"), (video or {}).get("duration"), (audio or {}).get("duration")):
        try:
            duration = float(candidate)
            if duration > 0:
                break
        except (TypeError, ValueError):
            continue

    size = 0
    try:
        size = int(container.get("size") or path.stat().st_size)
    except (TypeError, ValueError, OSError):
        size = 0

    return MediaProbe(
        path=path,
        duration=duration,
        width=int((video or {}).get("width") or 0),
        height=int((video or {}).get("height") or 0),
        size=size or (path.stat().st_size if path.exists() else 0),
        has_video=video is not None,
        has_audio=audio is not None,
        video_codec=str((video or {}).get("codec_name") or ""),
        audio_codec=str((audio or {}).get("codec_name") or ""),
        container=str(container.get("format_name") or ""),
    )


def has_faststart(path: Path, window: int = FASTSTART_SCAN_BYTES) -> bool:
    """True when the ``moov`` atom precedes ``mdat`` (streaming-friendly MP4)."""
    try:
        with path.open("rb") as handle:
            head = handle.read(window)
    except OSError:
        return False

    moov = head.find(b"moov")
    mdat = head.find(b"mdat")
    if moov == -1 and mdat == -1:
        return True  # not an MP4 container — nothing to reorder
    if mdat == -1:
        return True
    if moov == -1:
        return False
    return moov < mdat


async def ensure_faststart(
    path: Path,
    settings: Settings,
    *,
    registry: ProcessRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
) -> tuple[Path, bool]:
    """Move the MP4 index to the front when it is not already there."""
    if has_faststart(path):
        return path, True

    target = path.with_name(f"{path.stem}.faststart{path.suffix or '.mp4'}")
    command = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(path),
        "-c",
        "copy",
        "-movflags",
        "+faststart",
        str(target),
    ]
    result = await run_command(command, timeout=600, registry=registry, cancel_event=cancel_event, label="ffmpeg remux")
    if not result.ok or not target.exists() or target.stat().st_size == 0:
        log.warning("Could not remux for faststart: %s", result.stderr.strip()[:200])
        return path, False

    try:
        path.unlink(missing_ok=True)
        os.replace(target, path)
    except OSError:
        return target if target.exists() else path, has_faststart(target if target.exists() else path)
    return path, True


async def make_thumbnail(
    source: Path,
    destination: Path,
    settings: Settings,
    *,
    duration: float = 0.0,
    timestamp: float | None = None,
    registry: ProcessRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
) -> Path | None:
    """Extract a JPEG frame that satisfies Telegram's thumbnail rules."""
    max_side = min(settings.thumbnail_max_side, 320)
    when = timestamp if timestamp is not None else min(max(duration * 0.1, 1.0), 15.0)
    filter_scale = f"scale=w='if(gt(iw,{max_side}),{max_side},iw)':h=-2"

    for quality in ("4", "9"):
        command = [
            settings.ffmpeg_path,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-ss",
            f"{max(0.0, when):.2f}",
            "-i",
            str(source),
            "-frames:v",
            "1",
            "-vf",
            filter_scale,
            "-q:v",
            quality,
            "-f",
            "image2",
            str(destination),
        ]
        result = await run_command(
            command, timeout=120, registry=registry, cancel_event=cancel_event, label="ffmpeg thumbnail"
        )
        if not result.ok:
            log.warning("Thumbnail extraction failed: %s", result.stderr.strip()[:200])
            return None
        if destination.exists() and 0 < destination.stat().st_size <= THUMBNAIL_LIMIT_BYTES:
            return destination
        log.debug("Thumbnail over %s bytes, retrying with lower quality", THUMBNAIL_LIMIT_BYTES)

    return destination if destination.exists() else None


async def convert_audio(
    source: Path,
    destination: Path,
    settings: Settings,
    *,
    bitrate: str = "192k",
    registry: ProcessRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
) -> Path | None:
    """Transcode any audio stream into a Telegram-friendly ``.m4a``."""
    command = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source),
        "-vn",
        "-c:a",
        "aac",
        "-b:a",
        bitrate,
        "-movflags",
        "+faststart",
        str(destination),
    ]
    result = await run_command(command, timeout=900, registry=registry, cancel_event=cancel_event, label="ffmpeg audio")
    if not result.ok or not destination.exists() or destination.stat().st_size == 0:
        log.warning("Audio conversion failed: %s", result.stderr.strip()[:200])
        return None
    return destination


async def convert_thumbnail(source_image: Path, destination: Path, settings: Settings) -> Path | None:
    """Normalise a site-provided thumbnail (webp/png/jpg) into a small JPEG."""
    max_side = min(settings.thumbnail_max_side, 320)
    command = [
        settings.ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        str(source_image),
        "-vf",
        f"scale=w='if(gt(iw,{max_side}),{max_side},iw)':h=-2",
        "-q:v",
        "4",
        "-f",
        "image2",
        str(destination),
    ]
    result = await run_command(command, timeout=60, label="ffmpeg image")
    if not result.ok or not destination.exists() or destination.stat().st_size == 0:
        return None
    if destination.stat().st_size > THUMBNAIL_LIMIT_BYTES:
        return None
    return destination
