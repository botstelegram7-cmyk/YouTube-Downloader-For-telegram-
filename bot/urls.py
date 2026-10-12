"""Link handling: extraction, cleaning and a safety check before yt-dlp fetches.

The bot accepts arbitrary URLs, which means it can be told to fetch things that are
not on the public internet (cloud metadata endpoints, internal services). yt-dlp
would happily follow those, so every link is checked against the private address
spaces first.
"""

from __future__ import annotations

import asyncio
import ipaddress
import re
import socket
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

URL_RE = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)

#: Query parameters that only exist to track the share.
TRACKING_PARAMS = {
    "si",
    "feature",
    "pp",
    "ab_channel",
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "fbclid",
    "gclid",
    "igshid",
    "spm",
    "ref",
    "ref_src",
}

#: Hostnames that resolve to infrastructure rather than content.
BLOCKED_HOSTS = {
    "localhost",
    "localhost.localdomain",
    "metadata.google.internal",
    "metadata.goog",
    "instance-data",
    "metadata",
}

MAX_URL_LENGTH = 2048


def clean_url(url: str) -> str:
    """Drop tracking parameters and trailing punctuation from a URL."""
    url = (url or "").strip().strip("<>\"'")
    url = url.rstrip(").,;:!?")
    try:
        parts = urlsplit(url)
    except ValueError:
        return url

    if not parts.query:
        return urlunsplit(parts)

    query = [
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    return urlunsplit(parts._replace(query=urlencode(query)))


def extract_url(text: str) -> str | None:
    """Return the first http(s) URL in ``text``, cleaned, or ``None``."""
    if not text:
        return None
    match = URL_RE.search(text)
    if not match:
        return None
    url = clean_url(match.group(0))
    return url or None


def looks_like_url(text: str) -> bool:
    return extract_url(text) is not None


def ip_is_public(ip: str) -> bool:
    """True when an IP literal is routable on the public internet."""
    try:
        address = ipaddress.ip_address(ip)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return not (
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_multicast
        or address.is_reserved
        or address.is_unspecified
    )


def _resolve(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    return sorted({info[4][0] for info in infos})


def check_url_safety(url: str, resolver=_resolve, *, allow_private: bool = False) -> tuple[bool, str]:
    """Return ``(allowed, reason)`` for a URL before it is handed to yt-dlp.

    ``allow_private`` is for local testing only (the end-to-end harness serves its
    fixture on 127.0.0.1); it is off in every deployment.
    """
    if not url:
        return False, "Empty link."
    if len(url) > MAX_URL_LENGTH:
        return False, "That link is unusually long."

    try:
        parts = urlsplit(url)
    except ValueError:
        return False, "That link could not be parsed."

    if parts.scheme not in ("http", "https"):
        return False, "Only http and https links are supported."

    host = (parts.hostname or "").lower()
    if not host:
        return False, "That link has no hostname."

    if allow_private:
        return True, ""

    if host in BLOCKED_HOSTS:
        return False, "That host is not reachable from the public internet."

    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if not ip_is_public(str(literal)):
            return False, "That address points inside a private network."
        return True, ""

    addresses = resolver(host)
    if not addresses:
        return False, f"Could not resolve {host}."
    if any(not ip_is_public(address) for address in addresses):
        return False, "That hostname resolves to a private network address."
    return True, ""


async def check_url_safety_async(url: str, *, allow_private: bool = False) -> tuple[bool, str]:
    """``check_url_safety`` without blocking the event loop during DNS."""
    return await asyncio.to_thread(check_url_safety, url, allow_private=allow_private)
