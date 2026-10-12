"""Downloading with yt-dlp: progress, cancellation, muxing and verification."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from bot.errors import DownloadCancelled, DownloadError, TooLargeError
from bot.ffmpeg import (
    MediaProbe,
    convert_thumbnail,
    ensure_faststart,
    make_thumbnail,
    probe_media,
)
from bot.process import ProcessRegistry
from bot.qualities import QualityOption
from config import Settings

log = logging.getLogger("bot.download")

MEDIA_STEM = "media"
_IGNORED_SUFFIXES = {".part", ".ytdl", ".json", ".jpg", ".jpeg", ".png", ".webp", ".description"}


@dataclass
class ProgressUpdate:
    """One progress sample, sent from the download thread to the status message."""

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

    @property
    def resolution(self) -> str:
        return self.probe.resolution


def _download_params(settings: Settings, option: QualityOption, dest_dir: Path, hook) -> dict:
    params = {
        "format": option.format_selector,
        "outtmpl": str(dest_dir / f"{MEDIA_STEM}.%(ext)s"),
        "noplaylist": not settings.allow_playlist,
        "quiet": True,
        "no_warnings": True,
        "no_color": True,
        "noprogress": True,
        "consoletitle": False,
        "progress_hooks": [hook],
        "socket_timeout": settings.ytdlp_socket_timeout,
        "retries": settings.ytdlp_retries,
        "fragment_retries": settings.ytdlp_retries,
        "concurrent_fragment_downloads": settings.fragment_concurrency,
        "overwrites": True,
        "continuedl": True,
        "prefer_free_formats": settings.prefer_free_formats,
        "trim_file_name": 120,
        "restrictfilenames": False,
        "windowsfilenames": False,
        "writethumbnail": True,
        "merge_output_format": "mp4",
    }
    if settings.allow_playlist:
        params["playlist_items"] = "1"
    if settings.cookies_file:
        params["cookiefile"] = settings.cookies_file
    if settings.proxy:
        params["proxy"] = settings.proxy
    if settings.user_agent:
        params["http_headers"] = {"User-Agent": settings.user_agent}

    if option.is_audio:
        params["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": settings.audio_format,
                "preferredquality": "0",
                "nopostoverwrites": False,
            }
        ]
    else:
        # Merge the two streams and put the MP4 index at the front in one pass.
        params["postprocessor_args"] = {"ffmpeg": ["-movflags", "+faststart"]}
    return params


def _find_media_file(directory: Path) -> Path | None:
    """The biggest real media file in ``directory`` (ignores parts and images)."""
    candidates: list[tuple[int, Path]] = []
    for path in directory.iterdir():
        if not path.is_file() or path.name.startswith("."):
            continue
        if path.suffix.lower() in _IGNORED_SUFFIXES or ".part" in path.name or path.name.endswith(".ytdl"):
            continue
        if path.name.startswith(MEDIA_STEM):
            try:
                candidates.append((path.stat().st_size, path))
            except OSError:
                continue
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _find_thumbnail_file(directory: Path) -> Path | None:
    for path in sorted(directory.iterdir()):
        if (
            path.is_file()
            and path.name.startswith(MEDIA_STEM)
            and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}
        ):
            return path
    return None


def download_sync(
    url: str,
    *,
    settings: Settings,
    option: QualityOption,
    dest_dir: Path,
    on_progress: Callable[[ProgressUpdate], None],
    cancel_event: asyncio.Event,
) -> tuple[Path, dict]:
    """Blocking yt-dlp download with a progress/cancel hook (runs in a thread)."""
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError

    last_reported = {"downloaded": 0}

    def hook(payload: dict) -> None:
        if cancel_event.is_set():
            raise DownloadCancelled()

        status = payload.get("status")
        if status == "downloading":
            downloaded = int(payload.get("downloaded_bytes") or 0)
            total = payload.get("total_bytes") or payload.get("total_bytes_estimate")
            percent = None
            if total:
                percent = min(100.0, downloaded * 100.0 / float(total))
            on_progress(
                ProgressUpdate(
                    phase="download",
                    percent=percent,
                    downloaded=downloaded,
                    total=int(total) if total else None,
                    speed=payload.get("speed"),
                    eta=payload.get("eta"),
                )
            )
            last_reported["downloaded"] = downloaded
        elif status == "finished":
            on_progress(
                ProgressUpdate(
                    phase="convert",
                    percent=None,
                    downloaded=last_reported["downloaded"],
                    total=last_reported["downloaded"],
                    note="Muxing audio and video…",
                )
            )

    try:
        with YoutubeDL(_download_params(settings, option, dest_dir, hook)) as ydl:
            info = ydl.extract_info(url, download=True)
    except DownloadCancelled:
        raise
    except DownloadError as exc:
        from bot.probe import clean_error

        raise DownloadError(clean_error(str(exc))) from exc
    except Exception as exc:
        from bot.probe import clean_error

        raise DownloadError(clean_error(str(exc))) from exc

    if not info:
        raise DownloadError("The site returned no media for that link.")
    if info.get("_type") == "playlist" and info.get("entries"):
        entries = [entry for entry in info["entries"] if entry]
        info = entries[0] if entries else info
    return dest_dir, info


async def download_media(
    *,
    url: str,
    option: QualityOption,
    dest_dir: Path,
    settings: Settings,
    on_progress: Callable[[ProgressUpdate], None],
    cancel_event: asyncio.Event,
    registry: ProcessRegistry,
) -> DownloadResult:
    """Download, convert, verify and describe a media file.

    Raises :class:`DownloadCancelled`, :class:`TooLargeError` or
    :class:`DownloadError`; the caller turns those into messages.
    """
    dest_dir.mkdir(parents=True, exist_ok=True)
    on_progress(ProgressUpdate(phase="download", percent=0.0, note="Starting…"))

    _, info = await asyncio.to_thread(
        download_sync,
        url,
        settings=settings,
        option=option,
        dest_dir=dest_dir,
        on_progress=on_progress,
        cancel_event=cancel_event,
    )

    if cancel_event.is_set():
        raise DownloadCancelled()

    media_path = _find_media_file(dest_dir)
    if media_path is None:
        raise DownloadError("The download finished but produced no media file.")

    on_progress(ProgressUpdate(phase="convert", percent=None, note="Verifying the file…"))

    kind = "audio" if option.is_audio else "video"
    probe = await probe_media(media_path, settings, registry=registry, cancel_event=cancel_event)

    if kind == "audio" and settings.audio_format != "m4a":
        # mp3 is produced by yt-dlp's own postprocessor already.
        pass

    if kind == "audio" and not probe.has_audio:
        raise DownloadError("That link produced a file with no audio stream.")

    if kind == "video":
        if not probe.has_video:
            raise DownloadError("That link produced a file with no video stream.")
        media_path, faststart = await ensure_faststart(
            media_path, settings, registry=registry, cancel_event=cancel_event
        )
        probe = await probe_media(media_path, settings, registry=registry, cancel_event=cancel_event)
    else:
        faststart = True

    size = media_path.stat().st_size
    if size > settings.upload_limit_bytes:
        raise TooLargeError(size, settings.upload_limit_mb)

    thumbnail = await _prepare_thumbnail(
        dest_dir, media_path, probe, settings, kind=kind, registry=registry, cancel_event=cancel_event
    )

    title = str(info.get("title") or media_path.stem)
    artist = str(info.get("artist") or info.get("uploader") or info.get("channel") or "")
    if kind == "audio":
        title = str(info.get("track") or info.get("title") or title)

    return DownloadResult(
        path=media_path,
        title=title,
        duration=probe.duration or float(info.get("duration") or 0),
        width=probe.width,
        height=probe.height,
        size=size,
        kind=kind,
        quality_label=("Audio" if kind == "audio" else (f"{option.height}p" if option.height else "Best")),
        thumbnail=thumbnail,
        faststart=faststart,
        probe=probe,
        artist=artist,
    )


async def _prepare_thumbnail(
    dest_dir: Path,
    media_path: Path,
    probe: MediaProbe,
    settings: Settings,
    *,
    kind: str,
    registry: ProcessRegistry,
    cancel_event: asyncio.Event,
) -> Path | None:
    """A JPEG under 200 kB: the site's own artwork when possible, else a frame."""
    target = dest_dir / "thumb.jpg"

    source_image = _find_thumbnail_file(dest_dir)
    if source_image is not None:
        converted = await convert_thumbnail(source_image, target, settings)
        if converted is not None:
            log.debug("Using the site thumbnail for the upload")
            return converted

    if kind == "video" and probe.has_video:
        return await make_thumbnail(
            media_path,
            target,
            settings,
            duration=probe.duration,
            registry=registry,
            cancel_event=cancel_event,
        )
    return None
