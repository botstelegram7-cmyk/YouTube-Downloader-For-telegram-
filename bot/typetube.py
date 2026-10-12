"""The download engine: TypeTube.

TypeTube (https://github.com/botstelegram7-cmyk/typetube) resolves a YouTube link into
signed CDN stream URLs and downloads them with parallel range workers, then muxes video
and audio with FFmpeg. It ships a Node CLI, and the CLI is what this module drives:

    typetube resolve <url> --json      → full track metadata (title, streams, heights)
    typetube download <url> <dir> -v -f 720p -N 8 -x ...
                                       → download, with progress on stdout

Two properties of that CLI shape the code below, both verified against v1.1.4:

* **It always exits 0**, even when it fails — errors are printed to stderr as
  ``[Error] …``. Success is therefore detected by looking for the ``Completed: <path>``
  line, never by the exit code.
* **Progress is written with carriage returns** (``\\r[download] [phase] 42% of ~12.3MB
  at 8.10 MB/s``), so it must be read as a stream, not waited on.

It is also YouTube-only by design, and anything it does not recognise as a YouTube URL is
treated as a *search query* — a Vimeo link would silently download an unrelated video.
:func:`bot.urls.youtube_link` is what prevents that.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from bot.errors import DownloadError, ProbeError
from bot.process import StreamResult, run_command, run_streaming
from config import Settings

log = logging.getLogger("bot.typetube")

#: Node releases before this crash inside typetube's zstd import (zlib.zstdCompress).
MIN_NODE = (22, 15)
#: The only version this module was written and tested against.
TESTED_CLI = "1.1.4"

PROGRESS_RE = re.compile(
    r"\[download\]\s*(?:\[(?P<phase>[a-z]+)\]\s*)?(?P<percent>\d{1,3})%\s*of\s*~?(?P<total>[\d.]+)MB"
    r"(?:\s*at\s*(?P<speed>[\d.]+)\s*MB/s)?",
    re.IGNORECASE,
)
COMPLETED_RE = re.compile(r"Completed:\s*(?P<path>.+?)\s*\((?P<mb>[\d.]+)\s*MB in (?P<secs>[\d.]+)s\)")
ERROR_RE = re.compile(r"\[Error\]\s*(?P<message>.+)", re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Data
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Stream:
    """One stream offered by TypeTube."""

    itag: int | None
    quality: str  # "1080p", "107kbps Opus", …
    mime: str
    url: str = ""
    resolution: str = ""
    bitrate: int = 0
    hifi: bool = False

    @property
    def height(self) -> int | None:
        match = re.match(r"^(\d+)p", self.quality or "")
        if match:
            return int(match.group(1))
        if self.resolution and "x" in self.resolution:
            try:
                return int(self.resolution.split("x")[-1])
            except ValueError:
                return None
        return None

    @property
    def codec(self) -> str:
        lowered = (self.mime or "").lower()
        for name in ("av01", "vp9", "vp09", "avc1", "h264", "opus", "mp4a", "aac"):
            if name in lowered:
                return {"avc1": "H.264", "h264": "H.264", "vp09": "VP9", "mp4a": "AAC"}.get(name, name.upper())
        return ""

    @property
    def container(self) -> str:
        lowered = (self.mime or "").lower()
        if "webm" in lowered:
            return "webm"
        if "mp4" in lowered:
            return "mp4"
        return ""

    @property
    def telegram_safe(self) -> bool:
        """Telegram plays H.264/AAC in MP4 reliably; VP9/AV1 in MP4 often fails."""
        return self.container == "mp4" and self.codec in ("H.264", "")


@dataclass(frozen=True)
class Track:
    """What ``typetube resolve`` reports about one video."""

    id: str
    title: str
    author: str
    duration: float
    thumbnail: str
    video_streams: tuple[Stream, ...] = ()
    audio_streams: tuple[Stream, ...] = ()
    latency_ms: float = 0.0
    raw: dict = field(default_factory=dict, repr=False)
    url: str = ""

    @property
    def height(self) -> int | None:
        heights = [stream.height for stream in self.video_streams if stream.height]
        return max(heights) if heights else None

    def stream_for(self, height: int | None) -> Stream | None:
        """Best H.264/MP4 stream at or below ``height`` (``None`` = the best available)."""
        pool = [stream for stream in self.video_streams if stream.height]
        if not pool:
            return None
        safe = [stream for stream in pool if stream.telegram_safe] or pool
        if height is None:
            return max(safe, key=lambda stream: stream.height or 0)
        within = [stream for stream in safe if (stream.height or 0) <= height]
        if within:
            return max(within, key=lambda stream: stream.height or 0)
        return min(safe, key=lambda stream: stream.height or 0)

    def best_audio(self) -> Stream | None:
        if not self.audio_streams:
            return None
        return max(self.audio_streams, key=lambda stream: stream.bitrate or 0)


@dataclass
class DownloadProgress:
    """One progress line from the CLI."""

    phase: str  # downloading | muxing | complete | resolving
    percent: float
    total_mb: float = 0.0
    speed_mbps: float = 0.0
    downloaded_bytes: int = 0
    total_bytes: int = 0
    text: str = ""


@dataclass
class DownloadOutcome:
    """A finished download."""

    path: Path
    size_bytes: int
    duration_seconds: float
    title: str
    requested: str = ""


# --------------------------------------------------------------------------- #
# Toolchain
# --------------------------------------------------------------------------- #
def node_version(node_binary: str = "node") -> tuple[int, int] | None:
    """``(major, minor)`` of the Node runtime, or ``None`` when it is missing."""
    executable = shutil.which(node_binary) or (node_binary if os.path.isfile(node_binary) else None)
    if not executable:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    match = re.match(r"v(\d+)\.(\d+)", (completed.stdout or b"").decode("utf-8", "replace").strip())
    return (int(match.group(1)), int(match.group(2))) if match else None


def cli_command(settings: Settings) -> list[str]:
    """The argv prefix that runs the TypeTube CLI."""
    binary = settings.typetube_bin.strip()
    if binary.endswith((".js", ".mjs", ".cjs")):
        return [settings.node_binary, binary]
    return [binary]


def cli_present(settings: Settings) -> bool:
    """Whether the TypeTube CLI is actually installed (version 1.1.4 prints no version)."""
    binary = settings.typetube_bin.strip()
    if binary.endswith((".js", ".mjs", ".cjs")):
        return os.path.isfile(binary)
    return bool(shutil.which(binary))


def ensure_available(settings: Settings) -> list[str]:
    """Validate the toolchain; return human-readable problems (empty = all good)."""
    problems: list[str] = []

    if not cli_present(settings):
        problems.append(
            f"the TypeTube CLI ({settings.typetube_bin.strip()}) is not on PATH — install it with "
            f"`npm install -g typetube@{TESTED_CLI}` (Node {MIN_NODE[0]}.{MIN_NODE[1]}+)"
        )

    # The CLI muxes with an executable literally named "ffmpeg" on PATH.
    if os.path.basename(settings.ffmpeg_path) not in ("ffmpeg", "ffmpeg.exe"):
        problems.append(
            f"FFMPEG_BINARY is named {os.path.basename(settings.ffmpeg_path)!r}: the TypeTube CLI muxes with "
            "'ffmpeg' from PATH, so add a symlink named ffmpeg (or install FFmpeg normally)"
        )

    version = node_version(settings.node_binary)
    if version is None:
        problems.append(f"Node.js ({settings.node_binary}) was not found — TypeTube needs {MIN_NODE[0]}.{MIN_NODE[1]}+")
    elif version < MIN_NODE:
        problems.append(
            f"Node {version[0]}.{version[1]} is too old for TypeTube: it needs "
            f"{MIN_NODE[0]}.{MIN_NODE[1]}+ (older releases crash importing zstd)"
        )
    return problems


async def cli_version(settings: Settings) -> str | None:
    """Version reported by the installed CLI, or ``None``."""
    command = [*cli_command(settings), "--version"]
    try:
        result = await run_command(command, timeout=30, label="typetube --version", env=_client_env(settings))
    except Exception as exc:
        log.debug("Could not read the TypeTube version: %s", exc)
        return None
    text = (result.stdout or result.stderr or "").strip().splitlines()
    for line in text:
        if re.search(r"\d+\.\d+\.\d+", line):
            return line.strip()
    return text[0] if text else None


def _client_env(settings: Settings) -> dict[str, str]:
    """Environment for the CLI.

    The CLI reads its API key and cluster host from the environment, and it muxes with a
    plain ``ffmpeg`` looked up on ``PATH`` (it exposes no flag for the binary). So the
    directory of the configured FFmpeg is prepended to ``PATH``, which makes
    ``FFMPEG_BINARY=/opt/ffmpeg/ffmpeg`` work for the mux step too.
    """
    env = os.environ.copy()
    if settings.typetube_api_key:
        env["TYPETUBE_API_KEY"] = settings.typetube_api_key
    if settings.typetube_host:
        env["TYPETUBE_HOST"] = settings.typetube_host

    ffmpeg_dir = os.path.dirname(os.path.abspath(settings.ffmpeg_path))
    if ffmpeg_dir and os.path.isdir(ffmpeg_dir):
        env["PATH"] = ffmpeg_dir + os.pathsep + env.get("PATH", "")
    return env


def _clean_error(message: str) -> str:
    """Turn a TypeTube error into one readable sentence."""
    text = re.sub(r"\[TypeTube Error (\w+)\]:", r"(\1)", message).strip()
    text = text.replace("[TypeTube]", "").strip(" -")
    lowered = text.lower()
    if "403" in lowered or "regional" in lowered or "blocked" in lowered:
        return "TypeTube could not reach this video from the server's region (HTTP 403)."
    if "extraction_failed" in lowered or "failed to extract" in lowered:
        return "TypeTube could not extract playable streams for this video."
    if "quota" in lowered or "429" in lowered or "rate limit" in lowered:
        return "The TypeTube cluster is rate-limiting this server; try again in a minute."
    if "unavailable" in lowered or "private" in lowered or "not found" in lowered:
        return "That video is unavailable, private or removed."
    if "timeout" in lowered or "timed out" in lowered:
        return "TypeTube did not answer in time; try again."
    return text[:300] or "TypeTube reported an unknown error."


def _first_error(result: StreamResult) -> str | None:
    for line in (result.stderr or "").splitlines():
        match = ERROR_RE.search(line)
        if match:
            return _clean_error(match.group("message"))
    tail = (result.stdout or "").strip().splitlines()
    for line in reversed(tail):
        if "[Error]" in line:
            return _clean_error(line.split("[Error]", 1)[1])
    return None


# --------------------------------------------------------------------------- #
# Resolve
# --------------------------------------------------------------------------- #
def parse_track(payload: dict, url: str = "") -> Track:
    """Build a :class:`Track` from the CLI's JSON (also used by tests)."""
    if not isinstance(payload, dict) or not payload.get("id"):
        raise ProbeError("TypeTube returned no track for that link.")

    def stream(item: dict) -> Stream:
        return Stream(
            itag=item.get("itag"),
            quality=str(item.get("quality") or ""),
            mime=str(item.get("mimeType") or ""),
            url=str(item.get("url") or ""),
            resolution=str(item.get("resolution") or ""),
            bitrate=int(item.get("bitrate") or 0),
            hifi=bool(item.get("isHiFi")),
        )

    return Track(
        id=str(payload.get("id")),
        title=str(payload.get("title") or "video"),
        author=str(payload.get("author") or payload.get("uploader") or ""),
        duration=float(payload.get("durationSeconds") or 0),
        thumbnail=str(payload.get("thumbnail") or ""),
        video_streams=tuple(stream(item) for item in payload.get("videoStreams") or []),
        audio_streams=tuple(stream(item) for item in payload.get("audioStreams") or []),
        latency_ms=float((payload.get("meta") or {}).get("tLatencyMS") or payload.get("latencyMs") or 0),
        raw=payload,
        url=url,
    )


async def resolve(url: str, settings: Settings, *, timeout: float = 45.0) -> Track:
    """Resolve a YouTube link into streams and metadata (no download)."""
    command = [*cli_command(settings), "resolve", url, "--json"]
    try:
        result = await run_command(
            command,
            timeout=timeout,
            label="typetube resolve",
            env=_client_env(settings),
        )
    except Exception as exc:
        raise ProbeError(f"TypeTube could not be run: {exc}") from exc

    payload: dict | None = None
    text = (result.stdout or "").strip()
    start = text.find("{")
    if start >= 0:
        try:
            payload = json.loads(text[start:])
        except json.JSONDecodeError:
            payload = None

    if payload is None:
        raise ProbeError(_first_error(result) or "TypeTube returned no data for that link.")

    track = parse_track(payload, url)
    if not track.video_streams and not track.audio_streams:
        raise ProbeError("That video has no downloadable streams.")
    log.info(
        "Resolved %s (%s, %.0fs, %d video / %d audio streams) in %.0fms",
        track.id,
        track.title[:60],
        track.duration,
        len(track.video_streams),
        len(track.audio_streams),
        track.latency_ms,
    )
    return track


# --------------------------------------------------------------------------- #
# Download
# --------------------------------------------------------------------------- #
def parse_progress(segment: str) -> DownloadProgress | None:
    """Parse one ``[download]`` line (also used by tests)."""
    match = PROGRESS_RE.search(segment)
    if not match:
        return None
    total_mb = float(match.group("total") or 0)
    percent = float(match.group("percent") or 0)
    return DownloadProgress(
        phase=(match.group("phase") or "downloading").lower(),
        percent=percent,
        total_mb=total_mb,
        speed_mbps=float(match.group("speed") or 0),
        downloaded_bytes=int(total_mb * percent / 100 * 1024 * 1024),
        total_bytes=int(total_mb * 1024 * 1024),
        text=segment.strip(),
    )


def parse_completed(segments: list[str]) -> Path | None:
    """Find the ``Completed: <path>`` line the CLI prints on success."""
    for segment in reversed(segments):
        match = COMPLETED_RE.search(segment)
        if match:
            return Path(match.group("path").strip())
    return None


def download_command(
    settings: Settings,
    url: str,
    destination: Path,
    *,
    quality: str,
    audio_only: bool,
    audio_quality: str,
    codec: str,
    video_only: bool = False,
) -> list[str]:
    """Build the CLI argv for one download.

    ``video_only`` matters more than it looks: the engine's ``-v`` mode downloads a video
    *and* an audio stream and muxes them, and it refuses a video whose track has no audio at
    all. When the resolved track says there is no audio, ``--video-only`` is the only mode
    that can succeed.
    """
    command = [*cli_command(settings), "download", url, str(destination)]
    if audio_only:
        command += ["-x", "-a", audio_quality]
    elif video_only:
        command += ["--video-only", "-f", quality, "-c", codec, "--ext", "mp4"]
    else:
        command += ["-v", "-f", quality, "-a", audio_quality, "-c", codec, "--ext", "mp4"]
    command += ["-N", str(settings.typetube_workers), "--chunk-size", str(settings.typetube_chunk_mb)]
    return command


async def download(
    url: str,
    destination: Path,
    *,
    settings: Settings,
    quality: str = "highest",
    audio_only: bool = False,
    video_only: bool = False,
    codec: str = "avc1",
    registry=None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    timeout: float | None = None,
) -> DownloadOutcome:
    """Download one video (or its audio) with TypeTube.

    ``on_progress`` receives :class:`DownloadProgress` objects; cancellation terminates
    the CLI and its workers through the job's process registry.
    """
    destination.mkdir(parents=True, exist_ok=True)
    # Hi-Fi audio (256 kbps) is a private-key feature; public access tops out at ~160 kbps.
    audio_quality = "hifi" if settings.typetube_api_key else "highest"

    command = download_command(
        settings,
        url,
        destination,
        quality=quality,
        audio_only=audio_only,
        audio_quality=audio_quality,
        codec=codec,
        video_only=video_only,
    )
    log.info(
        "TypeTube download starting (quality=%s, audio_only=%s, video_only=%s)",
        quality,
        audio_only,
        video_only,
    )

    emitted: list[str] = []

    def handle(segment: str) -> None:
        emitted.append(segment)
        parsed = parse_progress(segment)
        if parsed and on_progress is not None:
            on_progress(parsed)

    result = await run_streaming(
        command,
        timeout=timeout or settings.download_timeout_seconds,
        registry=registry,
        cancel_event=cancel_event,
        label="typetube download",
        on_stdout=handle,
        env=_client_env(settings),
    )

    path = parse_completed(emitted) or parse_completed((result.stdout or "").splitlines())
    if path is None or not path.exists():
        message = _first_error(result) or "TypeTube did not report a completed file."
        raise DownloadError(message)

    stat = path.stat()
    completed = COMPLETED_RE.search("\n".join(emitted))
    duration = float(completed.group("secs")) if completed else 0.0
    log.info("TypeTube finished: %s (%.1f MB) in %.0fs", path.name, stat.st_size / 1e6, duration)
    return DownloadOutcome(
        path=path,
        size_bytes=stat.st_size,
        duration_seconds=duration,
        title=path.stem,
        requested=quality,
    )


# --------------------------------------------------------------------------- #
# Cache
# --------------------------------------------------------------------------- #
class TrackCache:
    """Remembers resolve results so a button press does not re-resolve the link.

    Keyed by a short token that travels in ``callback_data``; entries expire so a stale
    button can never start the wrong download.
    """

    def __init__(self, ttl_seconds: float = 3600, max_entries: int = 200) -> None:
        self.ttl = ttl_seconds
        self.max_entries = max_entries
        self._entries: dict[str, tuple[Track, float, int]] = {}

    def put(self, track: Track, *, user_id: int = 0) -> str:
        self._evict()
        token = uuid.uuid4().hex[:8]
        self._entries[token] = (track, time.monotonic(), user_id)
        return token

    def get(self, token: str, *, user_id: int | None = None) -> Track | None:
        entry = self._entries.get(token)
        if not entry:
            return None
        track, created, owner = entry
        if time.monotonic() - created > self.ttl:
            self._entries.pop(token, None)
            return None
        if user_id is not None and owner and owner != user_id:
            return None
        return track

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
