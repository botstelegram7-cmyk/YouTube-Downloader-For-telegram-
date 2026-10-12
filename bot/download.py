"""Turning a chosen quality into a verified, playable file.

The engine does the heavy lifting: TypeTube resolves the streams, pulls them down with
parallel range workers and muxes video + audio with FFmpeg. This module owns everything
after that, and it is where the promises to the user are kept:

* the delivered file really plays (ffprobe said so, not the metadata),
* MP4 is faststart so Telegram can stream it while it downloads,
* audio is M4A, converting only when the source is not,
* the thumbnail is a real JPEG under Telegram's 200 kB limit,
* a file that no available transport can carry is reported with Telegram's own number,
* the host's disk is checked before a job starts, and an out-of-space failure is reported
  as a disk problem rather than as a download failure.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot.errors import DiskSpaceError, DownloadCancelled, DownloadError, TooLargeError
from bot.ffmpeg import (
    THUMBNAIL_LIMIT_BYTES,
    MediaProbe,
    convert_audio,
    convert_thumbnail,
    ensure_faststart,
    make_thumbnail,
    probe_media,
)
from bot.process import ProcessRegistry
from bot.qualities import QualityOption
from bot.typetube import DownloadProgress, Track
from bot.typetube import download as typetube_download
from bot.utils import disk_free_bytes
from config import Settings

log = logging.getLogger("bot.download")

PHASE_ICONS = {"download": "⬇️", "convert": "🎛", "upload": "⬆️"}

#: Audio containers Telegram accepts for sendAudio without complaint.
AUDIO_KEEP = {".m4a", ".mp3"}


@dataclass
class ProgressUpdate:
    """One progress sample, handed to the status message."""

    phase: str
    percent: float | None = None
    downloaded: int = 0
    total: int | None = None
    speed: float | None = None
    eta: float | None = None
    note: str = ""


@dataclass
class DownloadResult:
    """A finished, verified media file ready to upload."""

    path: Path
    title: str
    duration: float
    width: int
    height: int
    size: int
    kind: str  # "video" | "audio"
    quality_label: str
    thumbnail: Path | None
    faststart: bool
    probe: MediaProbe
    artist: str = ""
    source_url: str = ""

    @property
    def resolution(self) -> str:
        return self.probe.resolution


def _bridge(on_progress: Callable[[ProgressUpdate], None]):
    """Translate TypeTube's progress into the status message's own vocabulary."""

    def handle(progress: DownloadProgress) -> None:
        phase = {"downloading": "download", "muxing": "convert", "complete": "download"}.get(progress.phase, "download")
        note = ""
        if progress.phase == "muxing":
            note = "Muxing video and audio…"
        elif progress.phase == "complete":
            note = "Download complete."
        percent = 100.0 if progress.phase == "complete" else progress.percent
        on_progress(
            ProgressUpdate(
                phase=phase,
                percent=percent,
                downloaded=progress.downloaded_bytes,
                total=progress.total_bytes or None,
                speed=(progress.speed_mbps * 1024 * 1024) or None,
                note=note,
            )
        )

    return handle


def _looks_like_disk_full(message: str) -> bool:
    """True when an engine error is really the filesystem saying it is full."""
    text = (message or "").lower()
    return any(
        marker in text
        for marker in ("enospc", "no space left", "not enough space", "disk full", "quota exceeded on disk")
    )


async def _audio_path(media: Path, settings: Settings, *, registry, cancel_event) -> Path:
    """Deliver audio as M4A: TypeTube may hand back Opus/WebM, which Telegram will not tag."""
    if media.suffix.lower() in AUDIO_KEEP:
        return media
    converted = await convert_audio(
        media,
        media.with_suffix(".m4a"),
        settings,
        registry=registry,
        cancel_event=cancel_event,
    )
    if converted is None:
        raise DownloadError("The audio track could not be converted to M4A.")
    log.info("Converted %s to M4A for delivery", media.name)
    return converted


async def _cover_for_audio(track: Track, target: Path, settings: Settings, *, registry, cancel_event) -> Path | None:
    """Cover art for an audio upload: the video's own still, resized to Telegram's limits."""
    import aiohttp

    if not track.thumbnail:
        return None
    raw = target.with_name("cover.jpg")
    try:
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(timeout=timeout) as session, session.get(track.thumbnail) as response:
            if response.status != 200:
                return None
            data = await response.read()
        if not data or len(data) > 4 * 1024 * 1024:
            return None
        raw.write_bytes(data)
    except Exception as exc:
        log.debug("Could not fetch cover art: %s", exc)
        return None

    converted = await convert_thumbnail(raw, target, settings)
    if converted is not None and converted.stat().st_size <= THUMBNAIL_LIMIT_BYTES:
        return converted
    return None


async def download_media(
    *,
    url: str,
    option: QualityOption,
    track: Track | None,
    dest_dir: Path,
    settings: Settings,
    on_progress: Callable[[ProgressUpdate], None],
    cancel_event: asyncio.Event,
    registry: ProcessRegistry,
) -> DownloadResult:
    """Download, verify and describe one file.

    Raises :class:`DownloadCancelled`, :class:`TooLargeError`, :class:`DiskSpaceError` or
    :class:`DownloadError`; the caller turns those into messages.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    kind = "audio" if option.is_audio else "video"

    # Hosting constraint, checked on its own terms: a job needs room for the file and its
    # remux, and failing with "disk full" halfway through helps nobody.
    free = disk_free_bytes(dest_dir)
    if free < settings.min_free_disk_bytes:
        raise DiskSpaceError(free, settings.min_free_disk_mb)

    # The engine downloads video and audio separately and muxes them, so it refuses a video
    # whose track has no audio at all. The resolved track says whether that is the case.
    video_only = bool(track is not None and not track.audio_streams)

    on_progress(ProgressUpdate(phase="download", percent=0.0, note="Starting…"))

    try:
        outcome = await typetube_download(
            url,
            dest_dir,
            settings=settings,
            quality=option.target or "highest",
            audio_only=option.is_audio,
            video_only=video_only,
            registry=registry,
            cancel_event=cancel_event,
            on_progress=_bridge(on_progress),
        )
    except DownloadError as exc:
        if _looks_like_disk_full(exc.reason):
            raise DiskSpaceError(disk_free_bytes(dest_dir), during="downloading") from exc
        raise

    if cancel_event.is_set():
        raise DownloadCancelled()

    media_path = outcome.path
    if not media_path.is_file() or media_path.stat().st_size < 1024:
        raise DownloadError("The download finished but produced no usable file.")

    on_progress(ProgressUpdate(phase="convert", percent=None, note="Checking the file…"))
    probe = await probe_media(media_path, settings, registry=registry, cancel_event=cancel_event)

    if kind == "audio":
        if not probe.has_audio:
            raise DownloadError("That link produced a file with no audio track.")
        media_path = await _audio_path(media_path, settings, registry=registry, cancel_event=cancel_event)
        probe = await probe_media(media_path, settings, registry=registry, cancel_event=cancel_event)
        faststart = True
    else:
        if not probe.has_video:
            raise DownloadError("That link produced a file with no video track.")
        on_progress(ProgressUpdate(phase="convert", percent=None, note="Preparing for streaming…"))
        media_path, faststart = await ensure_faststart(
            media_path, settings, registry=registry, cancel_event=cancel_event
        )
        probe = await probe_media(media_path, settings, registry=registry, cancel_event=cancel_event)

    # The only size check in the whole pipeline, and it quotes Telegram, not this app:
    # the file is compared with what the transport carrying it can actually send.
    size = media_path.stat().st_size
    if size > settings.upload_capability_bytes:
        raise TooLargeError(size, settings.upload_capability_mb)

    thumbnail: Path | None
    if kind == "video":
        thumbnail = await make_thumbnail(
            media_path,
            dest_dir / "thumb.jpg",
            settings,
            duration=probe.duration,
            registry=registry,
            cancel_event=cancel_event,
        )
    else:
        thumbnail = (
            await _cover_for_audio(
                track, dest_dir / "thumb.jpg", settings, registry=registry, cancel_event=cancel_event
            )
            if track
            else None
        )

    title = (track.title if track and track.title else outcome.title) or media_path.stem
    artist = track.author if track else ""

    return DownloadResult(
        path=media_path,
        title=title,
        duration=probe.duration or (track.duration if track else 0.0),
        width=probe.width,
        height=probe.height,
        size=size,
        kind=kind,
        quality_label=option.label,
        thumbnail=thumbnail,
        faststart=faststart,
        probe=probe,
        artist=artist,
        source_url=url,
    )
