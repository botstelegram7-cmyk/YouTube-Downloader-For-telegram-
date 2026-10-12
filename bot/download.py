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
import dataclasses
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot.errors import (
    CommandError,
    DiskSpaceError,
    DownloadCancelled,
    DownloadError,
    MissingDependency,
    ProbeError,
    TooLargeError,
)
from bot.fetch import FetchProgress, download_direct, download_torrent_file
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
from bot.sources import Source
from bot.torrent import TorrentError, parse_torrent_file
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
        speed = (progress.speed_mbps * 1024 * 1024) or None
        total = progress.total_bytes or None
        # How long is left, from the engine's own speed and total: an estimate, and labelled
        # as one. Without a total there is nothing to divide, so no ETA is shown at all.
        eta = None
        if speed and total:
            eta = max(0.0, (total - progress.downloaded_bytes) / speed)
        on_progress(
            ProgressUpdate(
                phase=phase,
                percent=percent,
                downloaded=progress.downloaded_bytes,
                total=total,
                speed=speed,
                eta=eta,
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


def _bridge_fetch(on_progress: Callable[[ProgressUpdate], None], note: str = ""):
    """Translate a fetcher sample into the status card's vocabulary."""

    def handle(sample: FetchProgress) -> None:
        on_progress(
            ProgressUpdate(
                phase="download",
                percent=sample.percent,
                downloaded=sample.downloaded,
                total=sample.total,
                speed=sample.speed,
                eta=sample.eta,
                note=note,
            )
        )

    return handle


async def download_from_source(
    *,
    source: Source,
    dest_dir: Path,
    settings: Settings,
    on_progress: Callable[[ProgressUpdate], None],
    cancel_event: asyncio.Event,
    registry: ProcessRegistry,
    track: Track | None = None,
    torrent_path: Path | None = None,
    torrent_index: int = 0,
) -> DownloadResult:
    """Fetch any non-YouTube source, then verify it exactly like a YouTube download.

    A torrent's file name decides how it is delivered (a video plays, an archive is sent as
    a document); a direct link is inspected with ffprobe and delivered the way its own
    content dictates. Either way the result is the same object the uploader already knows.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    free = disk_free_bytes(dest_dir)
    if free < settings.min_free_disk_bytes:
        raise DiskSpaceError(free, settings.min_free_disk_mb)

    awaited = "Torrent download" if source.is_torrent else "Download"
    on_progress(ProgressUpdate(phase="download", percent=0.0, note=f"{awaited} starting…"))

    if source.is_torrent:
        if torrent_path is None:
            raise DownloadError("That torrent's file list was lost — send the link again.")
        try:
            info = parse_torrent_file(torrent_path)
        except TorrentError as exc:
            raise DownloadError(exc.reason) from exc
        entry = info.file_number(torrent_index) or (info.candidates(1) or [None])[0]
        if entry is None:
            raise DownloadError("That torrent has no files to download.")
        path = await download_torrent_file(
            torrent_path=torrent_path,
            index=entry.index,
            destination=dest_dir,
            settings=settings,
            registry=registry,
            cancel_event=cancel_event,
            on_progress=_bridge_fetch(on_progress, f"Torrent: {entry.name}"),
        )
        title = entry.name
    else:
        path = await download_direct(
            source=source,
            destination=dest_dir,
            settings=settings,
            registry=registry,
            cancel_event=cancel_event,
            on_progress=_bridge_fetch(on_progress),
        )
        title = source.name or path.name

    return await finalise_download(
        path=path,
        dest_dir=dest_dir,
        settings=settings,
        on_progress=on_progress,
        cancel_event=cancel_event,
        registry=registry,
        title=title,
        source_url=source.url,
        fallback_author=source.label,
    )


#: Containers ffprobe understands well enough for its numbers to be trusted.
PLAYABLE_CONTAINERS = frozenset(
    {
        "3gp",
        "aac",
        "asf",
        "avi",
        "flv",
        "matroska",
        "m4a",
        "mov",
        "mp3",
        "mp4",
        "mpeg",
        "mpegts",
        "ogg",
        "wav",
        "webm",
    }
)
#: What ffprobe reports when it is guessing rather than reading real media.
GUESSED_CODECS = frozenset({"", "ansi", "bintext", "data", "none", "text"})

#: Container names ffprobe uses for audio-only files.
AUDIO_CONTAINERS = frozenset({"mp3", "m4a", "aac", "wav", "ogg"})


def looks_playable(probe: MediaProbe) -> tuple[bool, bool]:
    """``(is_video, is_audio)``, based on what ffprobe actually read.

    Point ffprobe at 64 kB of random bytes and it will happily report a ``bintext`` video
    stream 1280×3200 — a guess, not a fact. A random file must be delivered as a document,
    not remuxed, thumbnailed and announced as video, so a stream only counts when its codec
    and container are ones a player could really use.
    """
    # ffprobe reports a *list* of container names for a single file — an MP4 comes back as
    # "mov,mp4,m4a,3gp,3g2,mj2" — so any one of them being playable is enough.
    names = [part.strip() for part in (probe.container or "").lower().split(",") if part.strip()]
    recognised = not names or any(name in PLAYABLE_CONTAINERS for name in names)
    if not recognised:
        return False, False

    audio_only = bool(names) and all(name in AUDIO_CONTAINERS for name in names)
    video = bool(probe.has_video) and (probe.video_codec or "").lower() not in GUESSED_CODECS
    audio = bool(probe.has_audio) and bool(probe.audio_codec)
    if video and audio_only:
        video = False
    return video, audio


async def _probe_or_blank(
    path: Path,
    settings: Settings,
    *,
    registry: ProcessRegistry,
    cancel_event: asyncio.Event,
) -> MediaProbe:
    """Probe a file, treating "this is not media at all" as an answer rather than a failure.

    Point ffprobe at a ZIP and it exits non-zero; that is not a broken download, it is a
    document — and documents are delivered faithfully instead of being rejected.
    """
    try:
        return await probe_media(path, settings, registry=registry, cancel_event=cancel_event)
    except (CommandError, ProbeError) as exc:
        log.info("%s is not media (%s) — delivering it as a document", path.name, exc)
        return MediaProbe(
            path=path,
            duration=0.0,
            width=0,
            height=0,
            size=path.stat().st_size,
            has_video=False,
            has_audio=False,
            video_codec="",
            audio_codec="",
            container="",
        )


def _kind_for_suffix(name: str) -> str:
    """Video plays, audio plays, anything else is a document — decided by the file itself."""
    from bot.torrent import AUDIO_EXTENSIONS, VIDEO_EXTENSIONS

    suffix = Path(name).suffix.lower()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in AUDIO_EXTENSIONS:
        return "audio"
    return "document"


async def finalise_download(
    *,
    path: Path,
    dest_dir: Path,
    settings: Settings,
    on_progress: Callable[[ProgressUpdate], None],
    cancel_event: asyncio.Event,
    registry: ProcessRegistry,
    title: str,
    source_url: str = "",
    fallback_author: str = "",
) -> DownloadResult:
    """Verify a downloaded file, prepare it for Telegram, and describe it honestly.

    This is the contract the uploader relies on: the file is real (ffprobe said so), video
    is faststart, audio is M4A, there is a thumbnail when one can be made, and files that no
    transport can carry are refused with Telegram's own number.
    """
    if cancel_event.is_set():
        raise DownloadCancelled()
    if not path.is_file() or path.stat().st_size < 1024:
        raise DownloadError("The download finished but produced no usable file.")

    kind = _kind_for_suffix(path.name)
    on_progress(ProgressUpdate(phase="convert", percent=None, note="Checking the file…"))
    probe = await _probe_or_blank(path, settings, registry=registry, cancel_event=cancel_event)

    # A file whose name lies (an .mp4 that is really audio, a .bin that is really video) is
    # delivered as what it *is*: the probe decides, the extension only gets a vote.
    is_video, is_audio = looks_playable(probe)
    if kind != "document" and not (is_video or is_audio):
        kind = "document"
    if kind == "video" and not is_video:
        kind = "audio" if is_audio else "document"
    if kind == "audio" and not is_audio:
        kind = "video" if is_video else "document"

    if kind == "audio":
        path = await _audio_path(path, settings, registry=registry, cancel_event=cancel_event)
        probe = await probe_media(path, settings, registry=registry, cancel_event=cancel_event)
        faststart = True
    elif kind == "video":
        on_progress(ProgressUpdate(phase="convert", percent=None, note="Preparing for streaming…"))
        path, faststart = await ensure_faststart(path, settings, registry=registry, cancel_event=cancel_event)
        probe = await probe_media(path, settings, registry=registry, cancel_event=cancel_event)
    else:
        faststart = False
        if is_video:
            on_progress(ProgressUpdate(phase="convert", percent=None, note="Analyzing…"))
            try:
                path, faststart = await ensure_faststart(
                    path, settings, registry=registry, cancel_event=cancel_event
                )
                probe = await probe_media(path, settings, registry=registry, cancel_event=cancel_event)
            except (DownloadError, MissingDependency) as exc:
                log.debug("Remux skipped for the document (%s)", exc)

    size = path.stat().st_size
    if size > settings.upload_capability_bytes:
        raise TooLargeError(size, settings.upload_capability_mb)

    thumbnail: Path | None = None
    if is_video:
        try:
            thumbnail = await make_thumbnail(
                path,
                dest_dir / "thumb.jpg",
                settings,
                duration=probe.duration,
                registry=registry,
                cancel_event=cancel_event,
            )
        except Exception as exc:  # a thumbnail is a bonus, never a reason to fail
            log.debug("No thumbnail for %s: %s", path.name, exc)
            thumbnail = None

    duration = probe.duration or 0.0
    if kind == "document" and not (is_video or is_audio):
        # No readable media: no duration, no dimensions, no resolution — nothing invented.
        duration = 0.0
        probe = dataclasses.replace(probe, duration=0.0, width=0, height=0)

    return DownloadResult(
        path=path,
        title=title or path.stem,
        duration=duration,
        width=probe.width,
        height=probe.height,
        size=size,
        kind=kind,
        quality_label=_quality_label_for(kind, probe),
        thumbnail=thumbnail,
        faststart=faststart,
        probe=probe,
        artist=fallback_author,
        source_url=source_url,
    )


def _quality_label_for(kind: str, probe: MediaProbe) -> str:
    """How the picker's quality is shown for a file that had no picker."""
    if kind == "document":
        return "📦 File"
    if kind == "audio":
        return "🎵 Audio"
    return f"🎬 {probe.resolution}" if probe.resolution else "🎬 Video"


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
