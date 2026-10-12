"""Torrent files, read without a torrent library.

A ``.torrent`` is a bencoded dictionary. Two things are needed from it here — what is
inside and how big each part is — so this module carries a small, strict bencode decoder
(about sixty lines, no dependency) instead of pulling in a BitTorrent library.

The file list is what the user picks from, so the ordering matters: video first, then
audio, then everything else, and within each group the biggest files first. A scene
release's ``.nfo``, sample clips and subtitles are listed last, never first.
"""

from __future__ import annotations

import hashlib
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path

from bot.errors import BotError

log = logging.getLogger("bot.torrent")

#: Containers Telegram can be asked to play, in the order we would rather deliver them.
VIDEO_EXTENSIONS = (
    ".mp4",
    ".m4v",
    ".mkv",
    ".webm",
    ".mov",
    ".avi",
    ".flv",
    ".mpg",
    ".mpeg",
    ".ts",
    ".wmv",
    ".3gp",
)
AUDIO_EXTENSIONS = (".m4a", ".mp3", ".aac", ".flac", ".ogg", ".opus", ".wav", ".wma")

#: Scenery that ships with releases and is never what somebody wants to watch.
NOISE_EXTENSIONS = (".nfo", ".sfv", ".md5", ".url", ".txt", ".srt", ".sub", ".idx", ".exe")

_MAGNET_HASH = re.compile(r"xt=urn:btih:([A-Za-z0-9]{32,40})", re.IGNORECASE)
_MAGNET_NAME = re.compile(r"dn=([^&]+)", re.IGNORECASE)


class TorrentError(BotError):
    reason = "That torrent could not be read."


@dataclass(frozen=True)
class TorrentFile:
    """One file inside a torrent, numbered the way aria2 numbers it."""

    index: int  # 1-based: what ``aria2c --select-file`` expects
    path: str  # relative path inside the torrent
    size: int

    @property
    def name(self) -> str:
        return self.path.rsplit("/", 1)[-1]

    @property
    def suffix(self) -> str:
        name = self.name.lower()
        return name[name.rfind(".") :] if "." in name else ""

    @property
    def is_video(self) -> bool:
        return self.suffix in VIDEO_EXTENSIONS

    @property
    def is_audio(self) -> bool:
        return self.suffix in AUDIO_EXTENSIONS

    @property
    def is_media(self) -> bool:
        return self.is_video or self.is_audio

    @property
    def is_noise(self) -> bool:
        return self.suffix in NOISE_EXTENSIONS

    def rank(self) -> tuple[int, int]:
        """Sort key: videos, then audio, then the rest — and always the biggest first."""
        if self.is_video:
            group = 0
        elif self.is_audio:
            group = 1
        elif self.is_noise:
            group = 3
        else:
            group = 2
        return (group, -self.size)


@dataclass
class TorrentInfo:
    """Everything this bot needs to know about a torrent."""

    name: str
    files: list[TorrentFile] = field(default_factory=list)
    info_hash: str = ""
    announce: tuple[str, ...] = ()

    @property
    def total_size(self) -> int:
        return sum(item.size for item in self.files)

    def candidates(self, limit: int = 8) -> list[TorrentFile]:
        """The files worth offering, best first, capped for a readable keyboard."""
        ranked = sorted(self.files, key=lambda item: item.rank())
        return ranked[:limit]

    def file_number(self, index: int) -> TorrentFile | None:
        return next((item for item in self.files if item.index == index), None)


# -- bencode ------------------------------------------------------------------- #
def bdecode(data: bytes, *, _depth: int = 0):
    """Decode one bencoded value. Raises :class:`TorrentError` on anything malformed."""
    if _depth > 32:
        raise TorrentError("That torrent is nested too deeply to read.")
    try:
        value, position = _decode(data, 0, _depth)
    except TorrentError:
        raise
    except Exception as exc:  # pragma: no cover - defensive
        raise TorrentError(f"That torrent could not be decoded ({exc}).") from exc
    if position != len(data):
        raise TorrentError("That torrent has trailing data and is not a valid file.")
    return value


def _decode(data: bytes, position: int, depth: int):
    if position >= len(data):
        raise TorrentError("That torrent ends unexpectedly.")
    char = data[position : position + 1]

    if char == b"i":
        end = data.index(b"e", position)
        return int(data[position + 1 : end]), end + 1

    if char == b"l":
        position += 1
        items = []
        while data[position : position + 1] != b"e":
            item, position = _decode(data, position, depth + 1)
            items.append(item)
        return items, position + 1

    if char == b"d":
        position += 1
        result: dict = {}
        while data[position : position + 1] != b"e":
            key, position = _decode(data, position, depth + 1)
            value, position = _decode(data, position, depth + 1)
            result[key if isinstance(key, bytes) else str(key)] = value
        return result, position + 1

    colon = data.index(b":", position)
    length = int(data[position:colon])
    start = colon + 1
    return data[start : start + length], start + length


def _text(value) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", "replace")
    return str(value or "")


def parse_torrent(data: bytes) -> TorrentInfo:
    """Read the ``info`` dictionary of a ``.torrent`` payload."""
    decoded = bdecode(data)
    if not isinstance(decoded, dict) or b"info" not in decoded:
        raise TorrentError("That file is not a torrent (no info dictionary).")

    info = decoded[b"info"]
    if not isinstance(info, dict):
        raise TorrentError("That torrent's info dictionary is malformed.")

    name = _text(info.get(b"name")) or "torrent"
    files: list[TorrentFile] = []

    entries = info.get(b"files")
    if isinstance(entries, list):
        for position, entry in enumerate(entries, start=1):
            if not isinstance(entry, dict):
                continue
            size = entry.get(b"length") or 0
            parts = entry.get(b"path") or []
            path = "/".join(_text(part) for part in parts) or f"file-{position}"
            files.append(TorrentFile(index=position, path=path, size=int(size or 0)))
    else:
        # Single-file torrent: the name *is* the file, and aria2 calls it file 1.
        files.append(TorrentFile(index=1, path=name, size=int(info.get(b"length") or 0)))

    files = [item for item in files if item.size > 0]
    if not files:
        raise TorrentError("That torrent is empty.")

    announce = decoded.get(b"announce")
    announce_urls = [_text(announce)] if announce else []
    extra = decoded.get(b"announce-list")
    if isinstance(extra, list):
        for tier in extra:
            if isinstance(tier, list):
                announce_urls += [_text(item) for item in tier]

    return TorrentInfo(
        name=name,
        files=files,
        info_hash=_text(decoded.get(b"info_hash", b"")),
        announce=tuple(url for url in announce_urls if url),
    )


def parse_torrent_file(path: Path) -> TorrentInfo:
    try:
        return parse_torrent(Path(path).read_bytes())
    except OSError as exc:
        raise TorrentError(f"The torrent file could not be read ({exc.strerror or exc}).") from exc


def info_hash_of(payload: bytes) -> str:
    """The v1 infohash of a torrent file, which is also what aria2 names its metadata."""
    decoded = bdecode(payload)
    info = decoded.get(b"info") if isinstance(decoded, dict) else None
    if not isinstance(info, dict):
        raise TorrentError("That torrent has no info dictionary.")
    return hashlib.sha1(bencode(info)).hexdigest()


def bencode(value) -> bytes:
    """Encode a value the way a torrent file is encoded (the exact inverse of decoding)."""
    if isinstance(value, int):
        return f"i{value}e".encode()
    if isinstance(value, bytes):
        return f"{len(value)}:".encode() + value
    if isinstance(value, str):
        return bencode(value.encode())
    if isinstance(value, list):
        return b"l" + b"".join(bencode(item) for item in value) + b"e"
    if isinstance(value, dict):
        items = sorted(
            ((key, item) for key, item in value.items()),
            key=lambda pair: pair[0] if isinstance(pair[0], bytes) else str(pair[0]).encode(),
        )
        return b"d" + b"".join(bencode(key) + bencode(item) for key, item in items) + b"e"
    raise TorrentError(f"Cannot bencode {type(value).__name__}.")


# -- magnets -------------------------------------------------------------------- #
def magnet_hash(url: str) -> str:
    """The infohash inside a magnet link, lower-cased, or ``""``."""
    match = _MAGNET_HASH.search(url or "")
    return match.group(1).lower() if match else ""


def magnet_name(url: str) -> str:
    """The display name a magnet carries, decoded, or ``""``."""
    from urllib.parse import unquote_plus

    match = _MAGNET_NAME.search(url or "")
    if not match:
        return ""
    return unquote_plus(match.group(1)).strip()


def magnet_metadata_name(url: str) -> str:
    """The file aria2 writes metadata into for this magnet: ``<infohash>.torrent``.

    Base32 infohashes are converted to hex, because that is what aria2 uses for the name.
    """
    raw = magnet_hash(url)
    if len(raw) == 32:  # base32 (RFC 4648) — magnet links allow both encodings
        decoded = _base32_to_bytes(raw)
        return decoded.hex() + ".torrent"
    return raw + ".torrent"


def _base32_to_bytes(value: str) -> bytes:
    import base64

    padded = value.upper() + "=" * ((8 - len(value) % 8) % 8)
    try:
        return base64.b32decode(padded)
    except Exception as exc:
        raise TorrentError(f"That magnet link's infohash is malformed ({exc}).") from exc


__all__ = [
    "AUDIO_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "TorrentError",
    "TorrentFile",
    "TorrentInfo",
    "bdecode",
    "bencode",
    "info_hash_of",
    "magnet_hash",
    "magnet_metadata_name",
    "magnet_name",
    "parse_torrent",
    "parse_torrent_file",
]
