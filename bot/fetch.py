"""Fetching bytes from the open internet: magnets, torrents and direct links.

One tool does all three, and it is not written in Python: **aria2c**. It speaks BitTorrent
(DHT, magnets, web seeds, ``--select-file``), it fetches plain HTTP(S) URLs with several
parallel connections, and it is a single static binary already installed in the image.
Writing either of those by hand — a DHT client especially — would be thousands of lines of
code that no test could honestly cover.

Progress is read from two places, because either one alone can lie:

* aria2's console readout (``[#a1b2 4.2MiB/10MiB(42%) CN:5 DL:1.2MiB ETA:6s]``) knows the
  **total** size even when the server sends no ``Content-Length``,
* the growing files in the download directory know how many bytes are really on disk.

Everything here is cancellable through the job's process registry, and every path returned
sits inside the job's own temporary directory.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

from bot.errors import DownloadCancelled, DownloadError, MissingDependency
from bot.process import CommandError, run_command, run_streaming, which
from bot.torrent import TorrentError, magnet_metadata_name, parse_torrent_file
from config import Settings

log = logging.getLogger("bot.fetch")

#: aria2's per-session identifiers; the readout line always starts with one.
READOUT_RE = re.compile(
    r"\[#(?P<id>[0-9a-fA-F]+)\s+"
    r"(?P<done>\d+(?:\.\d+)?[KMGT]?i?B)\s*/\s*(?P<total>\d+(?:\.\d+)?[KMGT]?i?B|N/A)"
    r"\((?P<percent>\d+)%\)"
    r"(?P<rest>[^\]]*)\]"
)
SPEED_RE = re.compile(r"DL:(\d+(?:\.\d+)?[KMGT]?i?B)")
ETA_RE = re.compile(r"ETA:(\S+)")
SIZE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*([KMGT]?)(i?)B$", re.IGNORECASE)
_ETA_PART_RE = re.compile(r"(\d+(?:\.\d+)?)([dhms])", re.IGNORECASE)

#: aria2 writes these next to the payload; they are never part of the delivered file.
CONTROL_SUFFIXES = (".aria2", ".torrent")

#: Extensions a direct link is allowed to have for the file to keep its name.
_META_SUFFIXES = (".part", ".tmp")


@dataclass
class FetchProgress:
    """One sample of a running fetch, in the same vocabulary the status card uses."""

    percent: float | None
    downloaded: int
    total: int | None
    speed: float | None
    eta: float | None


def size_to_bytes(text: str) -> int:
    """``"1.2MiB"`` → ``1258291``. aria2 mixes binary and decimal suffixes; both are read."""
    match = SIZE_RE.match((text or "").strip())
    if not match:
        return 0
    value = float(match.group(1))
    unit = match.group(2).upper()
    # "1.2MiB" is binary, "1.5MB" decimal — aria2 prints both, depending on the source.
    step = 1024 if match.group(3) else 1000
    power = {"": 0, "K": 1, "M": 2, "G": 3, "T": 4}[unit]
    return int(value * step**power)


def parse_eta(text: str) -> float | None:
    """``"3m11s"`` → ``191.0``; ``""`` → ``None``."""
    if not text:
        return None
    total = 0.0
    matched = False
    for value, unit in _ETA_PART_RE.findall(text):
        matched = True
        total += float(value) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit.lower()]
    return total if matched else None


def parse_readout(line: str) -> FetchProgress | None:
    """Read one console-readout line; ``None`` when the line is not one."""
    match = READOUT_RE.search(line or "")
    if not match:
        return None
    rest = match.group("rest") or ""
    speed_match = SPEED_RE.search(rest)
    eta_match = ETA_RE.search(rest)
    total_text = match.group("total")
    total = size_to_bytes(total_text) if "/" not in total_text and total_text.upper() != "N/A" else None
    downloaded = size_to_bytes(match.group("done"))
    speed = size_to_bytes(speed_match.group(1)) if speed_match else None
    eta = parse_eta(eta_match.group(1)) if eta_match else None
    return FetchProgress(
        percent=float(match.group("percent")),
        downloaded=downloaded,
        total=total,
        speed=float(speed) if speed else None,
        eta=eta,
    )


# -- the binary ----------------------------------------------------------------- #


def aria2_binary(settings: Settings) -> str:
    return (settings.aria2_path or "aria2c").strip() or "aria2c"


def aria2_present(settings: Settings) -> bool:
    return which(aria2_binary(settings)) is not None


async def aria2_version(settings: Settings) -> str | None:
    """First line of ``aria2c --version``, or ``None``. Used by ``/diag`` and ``--check``."""
    try:
        result = await run_command([aria2_binary(settings), "--version"], timeout=10, label="aria2c --version")
    except (CommandError, FileNotFoundError, OSError):
        return None
    line = (result.stdout or "").splitlines()[0].strip() if result.stdout else ""
    return line.split()[-1] if line.startswith("aria2 version") else (line or None)


def _base_arguments(settings: Settings, destination: Path) -> list[str]:
    destination.mkdir(parents=True, exist_ok=True)
    return [
        "--dir",
        str(destination),
        # The workspace is temporary and often a container filesystem: never preallocate,
        # never keep failed leftovers, never ask before overwriting.
        "--file-allocation=none",
        "--allow-overwrite=true",
        "--auto-file-renaming=false",
        "--always-resume=false",
        "--continue=false",
        "--max-tries=5",
        "--retry-wait=3",
        "--connect-timeout=20",
        "--timeout=60",
        "--summary-interval=0",
        "--show-console-readout=true",
        "--console-log-level=warn",
        "--no-conf=true",
        "--quiet=false",
    ]


def _torrent_arguments() -> list[str]:
    return [
        # Download what was asked for, then stop: a bot is not a seedbox.
        "--seed-time=0",
        "--bt-stop-timeout=120",
        "--bt-max-peers=100",
        "--bt-enable-lpd=false",
        "--enable-dht=true",
        "--follow-torrent=true",
        "--check-integrity=false",
        "--max-concurrent-downloads=1",
    ]


def _clean_error(text: str) -> str:
    """Strip aria2's escape codes and keep the human sentence."""
    plain = re.sub(r"\x1b\[[0-9;]*m", "", text or "")
    for line in plain.splitlines():
        stripped = line.strip()
        if stripped and "errorCode=" in stripped:
            tail = stripped.split("errorCode=", 1)[1]
            tail = tail.split("]", 1)[-1].strip()
            return tail or stripped
    return (plain.strip().splitlines()[-1] if plain.strip() else "")[:300]


def _failed(result, what: str) -> DownloadError:
    detail = _clean_error(result.stderr) or _clean_error(result.stdout)
    return DownloadError(f"{what} failed. {detail}".strip())


# -- metadata ------------------------------------------------------------------- #


async def fetch_torrent_metadata(
    *,
    source,
    destination: Path,
    settings: Settings,
    registry=None,
    cancel_event: asyncio.Event | None = None,
    timeout: float = 180.0,
) -> Path:
    """Get the ``.torrent`` for a magnet or a ``.torrent`` URL, inside ``destination``."""
    binary = aria2_binary(settings)
    if which(binary) is None:
        raise MissingDependency(
            f"The fetcher ({binary}) is not installed, so torrents and direct links are "
            "unavailable. Add aria2 to the image, or set ARIA2_BINARY."
        )

    if not source.is_magnet:
        # A plain .torrent URL: aria2 fetches the file itself and nothing else. Without
        # --follow-torrent=false it would start downloading the *payload* too (and then keep
        # seeding it), which is exactly what the user has not chosen yet.
        command = [
            binary,
            *_base_arguments(settings, destination),
            "--follow-torrent=false",
            "--bt-metadata-only=false",
            source.url,
        ]
        try:
            result = await run_command(
                command, timeout=timeout, registry=registry, cancel_event=cancel_event, label="aria2c metadata"
            )
        except CommandError as exc:
            raise DownloadError("Fetching the torrent file took too long." if exc.timed_out else str(exc)) from exc
        if cancel_event is not None and cancel_event.is_set():
            raise DownloadCancelled()
        if not result.ok:
            raise _failed(result, "Fetching the torrent file")
        candidates = [
            path
            for path in sorted(destination.iterdir())
            if path.is_file() and path.suffix.lower() == ".torrent" and path.stat().st_size > 0
        ]
        if not candidates:
            raise DownloadError("That link did not produce a torrent file.")
        return candidates[0]

    expected = destination / magnet_metadata_name(source.url)
    command = [
        binary,
        *_base_arguments(settings, destination),
        *_torrent_arguments(),
        "--bt-metadata-only=true",
        "--bt-save-metadata=true",
        "--dht-listen-port=6881-6999",
        source.url,
    ]
    try:
        result = await run_command(
            command, timeout=timeout, registry=registry, cancel_event=cancel_event, label="aria2c magnet"
        )
    except CommandError as exc:
        if exc.timed_out:
            raise DownloadError(
                "No peers answered for that magnet link within three minutes. "
                "Try a torrent that has active seeders, or send the .torrent file directly."
            ) from exc
        raise DownloadError(str(exc)) from exc
    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled()

    if expected.exists() and expected.stat().st_size > 0:
        return expected
    # aria2 names the metadata after the infohash; trust the directory over the guess.
    found = [
        path
        for path in sorted(destination.iterdir())
        if path.is_file() and path.suffix.lower() == ".torrent" and path.stat().st_size > 0
    ]
    if found:
        return found[0]
    raise _failed(result, "Reading that magnet link")


# -- payload -------------------------------------------------------------------- #


def _payload_sizes(destination: Path) -> int:
    """Bytes that aria2 has written for the payload (control files excluded)."""
    total = 0
    for path in destination.rglob("*"):
        try:
            if path.is_file() and not path.name.endswith(CONTROL_SUFFIXES):
                total += path.stat().st_size
        except OSError:
            continue
    return total


async def _monitor(
    destination: Path,
    *,
    total: int | None,
    on_progress,
    interval: float = 1.0,
    state: dict | None = None,
):
    """Report progress from the files on disk until cancelled.

    ``state`` carries the total and speed that the console readout learned, so the two
    sources enrich each other instead of fighting.
    """
    state = state if state is not None else {}
    previous_bytes = 0
    previous_time = time.monotonic()
    measured_speed: float | None = None

    while True:
        await asyncio.sleep(interval)
        done = _payload_sizes(destination)
        if state.get("total"):
            total = state["total"]
        now = time.monotonic()
        elapsed = now - previous_time
        if elapsed > 0 and done >= previous_bytes:
            instant = (done - previous_bytes) / elapsed
            measured_speed = instant if measured_speed is None else (measured_speed * 0.7 + instant * 0.3)
        previous_bytes, previous_time = done, now

        speed = state.get("speed") or measured_speed
        percent = None
        if total:
            percent = min(100.0, done * 100.0 / total)
        eta = None
        if total and speed:
            eta = max(0.0, (total - done) / speed)

        on_progress(
            FetchProgress(
                percent=percent,
                downloaded=done,
                total=total,
                speed=speed,
                eta=eta,
            )
        )


async def _run_fetch(
    command: list[str],
    *,
    destination: Path,
    settings: Settings,
    label: str,
    what: str,
    registry=None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    timeout: float | None = None,
) -> None:
    """Run aria2 with a progress monitor attached, and turn failures into BotErrors."""
    state: dict = {}
    emitted: list[str] = []

    def handle(segment: str) -> None:
        emitted.append(segment)
        parsed = parse_readout(segment)
        if parsed is None:
            return
        if parsed.total:
            state["total"] = parsed.total
        if parsed.speed:
            state["speed"] = parsed.speed
        if on_progress is not None:
            on_progress(parsed)

    monitor = (
        asyncio.create_task(_monitor(destination, total=None, on_progress=on_progress, state=state))
        if on_progress is not None
        else None
    )
    try:
        result = await run_streaming(
            command,
            timeout=timeout or settings.download_timeout_seconds,
            registry=registry,
            cancel_event=cancel_event,
            label=label,
            on_stdout=handle,
        )
    except CommandError as exc:
        raise DownloadError(
            f"{what} took longer than {human_timeout(timeout or settings.download_timeout_seconds)} and was stopped."
            if exc.timed_out
            else str(exc)
        ) from exc
    finally:
        if monitor is not None:
            monitor.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await monitor

    if cancel_event is not None and cancel_event.is_set():
        raise DownloadCancelled()
    if not result.ok:
        raise _failed(result, what)
    if on_progress is not None:
        done = _payload_sizes(destination)
        on_progress(
            FetchProgress(percent=100.0, downloaded=done, total=done or None, speed=None, eta=0.0)
        )


def human_timeout(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} hours"
    if seconds >= 60:
        return f"{seconds / 60:.0f} minutes"
    return f"{seconds:.0f} seconds"


def _newest_payload(destination: Path, *, since: float = 0.0) -> Path | None:
    """The largest payload file in ``destination`` written after ``since``."""
    best: Path | None = None
    for path in destination.rglob("*"):
        try:
            if not path.is_file() or path.name.endswith(CONTROL_SUFFIXES):
                continue
            if since and path.stat().st_mtime < since - 1:
                continue
            if best is None or path.stat().st_size > best.stat().st_size:
                best = path
        except OSError:
            continue
    return best


async def download_direct(
    *,
    source,
    destination: Path,
    settings: Settings,
    registry=None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    timeout: float | None = None,
) -> Path:
    """Fetch one direct HTTP(S) link with parallel connections."""
    binary = aria2_binary(settings)
    if which(binary) is None:
        raise MissingDependency(
            f"The fetcher ({binary}) is not installed, so direct links are unavailable. "
            "Add aria2 to the image, or set ARIA2_BINARY."
        )

    started = time.time()
    name = source.name or ""
    command = [
        binary,
        *_base_arguments(settings, destination),
        # aria2's own default for parallel HTTP is 5 connections per server; 16 is what
        # makes a single big file use the whole link instead of one TCP stream's worth.
        "--max-connection-per-server=16",
        "--split=16",
        "--min-split-size=1M",
        "--max-concurrent-downloads=1",
        "--content-disposition=true",
        "--remote-time=true",
        "--user-agent=Mozilla/5.0 (compatible; ytbot/1.0)",
    ]
    if name:
        command += ["--out", name]
    command.append(source.url)

    await _run_fetch(
        command,
        destination=destination,
        settings=settings,
        label="aria2c direct",
        what="That download",
        registry=registry,
        cancel_event=cancel_event,
        on_progress=on_progress,
        timeout=timeout,
    )

    path = None
    if name:
        candidate = destination / name
        if candidate.is_file() and candidate.stat().st_size > 0:
            path = candidate
    if path is None:
        path = _newest_payload(destination, since=started)
    if path is None:
        raise DownloadError("That link returned no file.")
    if path.suffix.lower() in _META_SUFFIXES:
        raise DownloadError("That link returned an unfinished download.")
    return path


async def download_torrent_file(
    *,
    torrent_path: Path,
    index: int,
    destination: Path,
    settings: Settings,
    registry=None,
    cancel_event: asyncio.Event | None = None,
    on_progress=None,
    timeout: float | None = None,
) -> Path:
    """Download one file (by aria2's 1-based index) out of a torrent, then stop seeding."""
    binary = aria2_binary(settings)
    if which(binary) is None:
        raise MissingDependency(f"The fetcher ({binary}) is not installed, so torrents are unavailable.")

    try:
        info = parse_torrent_file(torrent_path)
    except TorrentError as exc:
        raise DownloadError(exc.reason) from exc

    wanted = info.file_number(index)
    if wanted is None:
        raise DownloadError("That file is not in the torrent any more.")

    started = time.time()
    command = [
        binary,
        *_base_arguments(settings, destination),
        *_torrent_arguments(),
        "--select-file",
        str(index),
        "--dht-listen-port=6881-6999",
        str(torrent_path),
    ]

    await _run_fetch(
        command,
        destination=destination,
        settings=settings,
        label="aria2c torrent",
        what="That torrent download",
        registry=registry,
        cancel_event=cancel_event,
        on_progress=on_progress,
        timeout=timeout,
    )

    expected = destination / wanted.path
    if expected.is_file() and expected.stat().st_size > 0:
        return expected
    # Multi-file torrents keep their directory structure; find the file by its own name.
    matches = [
        path
        for path in destination.rglob(wanted.name)
        if path.is_file() and not path.name.endswith(CONTROL_SUFFIXES) and path.stat().st_mtime >= started - 1
    ]
    if matches:
        return max(matches, key=lambda path: path.stat().st_size)
    fallback = _newest_payload(destination, since=started)
    if fallback is None or fallback.stat().st_size <= 0:
        raise DownloadError("The torrent finished but produced no file.")
    log.warning("Fell back to %s for torrent file #%s", fallback.name, index)
    return fallback


@dataclass
class LinkProbe:
    """What a HEAD request can honestly say about a direct link before downloading it."""

    total: int | None = None
    filename: str = ""
    content_type: str = ""
    reachable: bool = False
    error: str = ""


async def probe_link(url: str, *, timeout: float = 15.0) -> LinkProbe:
    """Ask the server for the file's name and size — best effort, never fatal.

    Servers refuse HEAD, omit ``Content-Length``, or answer 403 to anything but a browser;
    none of that is a reason to refuse the download, so failures come back as an unreachable
    probe with the reason attached, and the caller decides whether to care.
    """
    import aiohttp

    try:
        client_timeout = aiohttp.ClientTimeout(total=timeout, connect=timeout / 2)
        headers = {"User-Agent": "Mozilla/5.0 (compatible; ytbot/1.0)"}
        # A real client's Range request: many CDNs reject a bare HEAD but answer this.
        async with (
            aiohttp.ClientSession(timeout=client_timeout, headers=headers) as session,
            session.get(url, headers={**headers, "Range": "bytes=0-0"}) as response,
        ):
                if response.status >= 400:
                    return LinkProbe(reachable=False, error=f"HTTP {response.status}")
                length = response.headers.get("Content-Length")
                total = int(length) if length and length.isdigit() else None
                if total == 1 or (total is not None and response.status == 206):
                    # "Range: bytes=0-0" answers 1 byte with a Content-Range total.
                    content_range = response.headers.get("Content-Range", "")
                    if "/" in content_range:
                        tail = content_range.rsplit("/", 1)[-1]
                        total = int(tail) if tail.isdigit() else None
                return LinkProbe(
                    total=total,
                    filename=filename_from_headers(response),
                    content_type=response.headers.get("Content-Type", ""),
                    reachable=True,
                )
    except Exception as exc:
        return LinkProbe(reachable=False, error=str(exc)[:160])


def filename_from_headers(response) -> str:
    """The file name the server suggests: ``Content-Disposition`` first, then the path."""
    from urllib.parse import unquote

    disposition = response.headers.get("Content-Disposition", "") or ""
    for marker in ("filename*=", "filename="):
        if marker in disposition:
            value = disposition.split(marker, 1)[1].split(";", 1)[0].strip().strip('"')
            if marker == "filename*=" and value.lower().startswith("utf-8''"):
                value = value[7:]
            return unquote(value).strip()
    return ""


__all__ = [
    "CONTROL_SUFFIXES",
    "FetchProgress",
    "LinkProbe",
    "aria2_binary",
    "aria2_present",
    "aria2_version",
    "download_direct",
    "download_torrent_file",
    "fetch_torrent_metadata",
    "parse_eta",
    "parse_readout",
    "probe_link",
    "size_to_bytes",
]
