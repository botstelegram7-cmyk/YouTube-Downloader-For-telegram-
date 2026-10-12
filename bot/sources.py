"""What kind of link is this, and may the bot fetch it?

Three kinds are understood, and each has its own path through the bot:

* **YouTube** — resolved by the engine, offered as a quality picker (the only source where
  the user has something to choose).
* **Torrent** — a ``magnet:`` link or a URL to a ``.torrent`` file. Metadata is fetched
  first, then the user picks which file inside it to download.
* **Direct** — any other ``http(s)`` link to a file. Downloaded as-is; if it turns out to be
  video or audio it is delivered as such, otherwise as a document.

The guard at the bottom is deliberate: a bot that will fetch *any* URL a stranger sends is
a request forgery tool aimed at whatever network it runs in (cloud metadata endpoints,
internal dashboards, other containers). Loopback, private, link-local and reserved
addresses are refused unless the operator explicitly turns that off for testing.
"""

from __future__ import annotations

import ipaddress
import logging
from dataclasses import dataclass
from pathlib import PurePosixPath
from urllib.parse import unquote, urlparse

from bot.urls import VIDEO_ID_RE, canonise, extract_url
from config import Settings

log = logging.getLogger("bot.sources")

YOUTUBE = "youtube"
TORRENT = "torrent"
DIRECT = "direct"

#: Host suffixes that are never a public file server.
BLOCKED_SUFFIXES = (".localhost", ".local", ".internal", ".home.arpa", ".in-addr.arpa")
BLOCKED_HOSTS = ("localhost", "localhost.localdomain", "metadata.google.internal")

#: Extensions that make a URL obviously a torrent without downloading it.
TORRENT_EXTENSIONS = (".torrent",)


@dataclass(frozen=True)
class Source:
    """A link the bot has accepted, and where it will get the bytes."""

    kind: str
    url: str
    label: str = ""  # short, user-facing: the host
    name: str = ""  # suggested title / filename
    is_magnet: bool = False

    @property
    def is_youtube(self) -> bool:
        return self.kind == YOUTUBE

    @property
    def is_torrent(self) -> bool:
        return self.kind == TORRENT

    @property
    def is_direct(self) -> bool:
        return self.kind == DIRECT

    def describe(self) -> str:
        return self.label or self.url[:60]


def filename_from_url(url: str) -> str:
    """The file name a URL points at, percent-decoded, or ``""``."""
    try:
        path = urlparse(url).path
    except ValueError:
        return ""
    name = unquote(PurePosixPath(path).name).strip()
    return name if name and "." in name else ""


def is_torrent_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
    except ValueError:
        return False
    path = unquote(parsed.path).lower()
    return path.endswith(TORRENT_EXTENSIONS)


def blocked_target(url: str, settings: Settings) -> str:
    """``""`` when the bot may fetch this URL, otherwise why it will not.

    Only *literal* addresses are judged here — a public hostname that resolves to a private
    address is resolved by aria2, not by this process, and is left to the operator's own
    network policy.
    """
    if settings.allow_private_links:
        return ""

    try:
        parsed = urlparse(url)
    except ValueError:
        return "That link could not be understood."

    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        return "That link has no host to fetch from."
    if host in BLOCKED_HOSTS or host.endswith(BLOCKED_SUFFIXES):
        return "That link points inside the server's own network."

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return ""  # a normal hostname; nothing more to judge without resolving it

    if (
        address.is_loopback
        or address.is_private
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    ):
        return "That link points inside the server's own network."
    return ""


def classify(text: str, settings: Settings) -> tuple[Source | None, str]:
    """``(source, refusal)`` for a message.

    ``refusal`` is user-facing text when a link was found but cannot be served; both are
    empty when the message simply was not a link.
    """
    text = (text or "").strip()
    if not text:
        return None, ""

    if text.lower().startswith("magnet:"):
        from bot.torrent import magnet_hash

        if not magnet_hash(text):
            return None, "That magnet link has no infohash in it."
        from bot.torrent import magnet_name

        return Source(kind=TORRENT, url=text, label="magnet link", name=magnet_name(text), is_magnet=True), ""

    url = extract_url(text)
    if url is not None:
        canonical = canonise(url)
        if canonical:
            return Source(kind=YOUTUBE, url=canonical, label="YouTube"), ""

        refusal = blocked_target(url, settings)
        if refusal:
            return None, refusal

        if is_torrent_url(url):
            return (
                Source(kind=TORRENT, url=url, label=urlparse(url).hostname or "torrent", name=filename_from_url(url)),
                "",
            )

        host = (urlparse(url).hostname or url).lower()
        if host.startswith("www."):
            host = host[4:]
        return Source(kind=DIRECT, url=url, label=host, name=filename_from_url(url)), ""

    if VIDEO_ID_RE.match(text):
        return Source(kind=YOUTUBE, url=f"https://www.youtube.com/watch?v={text}", label="YouTube"), ""

    if "magnet:" in text.lower():
        return None, "Send the magnet link on its own — I could not find one in that message."

    return None, ""


__all__ = [
    "BLOCKED_HOSTS",
    "BLOCKED_SUFFIXES",
    "DIRECT",
    "TORRENT",
    "YOUTUBE",
    "Source",
    "blocked_target",
    "classify",
    "filename_from_url",
    "is_torrent_url",
]
