"""Link handling: pull the link out of a message and prove it is one we can serve.

Two rules matter here.

**Quote the right risk.** The engine (TypeTube) only understands YouTube, and it treats
anything it does not recognise as a *search query* — a Vimeo link would quietly download an
unrelated YouTube video. So every link is checked and canonicalised before it reaches the
engine, and everything else is refused with an explanation.

**Canonicalise, don't guess.** ``youtu.be/ID``, ``/shorts/ID``, ``/live/ID``, ``/embed/ID``,
``music.youtube.com`` and playlist-laden watch URLs all become one canonical
``https://www.youtube.com/watch?v=ID``. A bare 11-character video id is accepted too, since
that is what people copy from the app.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

#: Literal IPv6 hosts are matched as a unit, so ``http://[::1]/x`` is not truncated at the
#: closing bracket; everything else stops at whitespace, angle brackets or a stray bracket.
URL_RE = re.compile(r"https?://(?:\[[0-9A-Fa-f:.]+\]|[^\s<>\"')\]]+)", re.IGNORECASE)
VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

#: Hosts whose paths are understood (``www.`` is optional on all of them).
YOUTUBE_HOSTS = (
    "youtube.com",
    "m.youtube.com",
    "music.youtube.com",
    "youtube-nocookie.com",
    "youtu.be",
)

#: Path prefixes that carry a video id as the next segment.
ID_PATHS = ("shorts", "live", "embed", "v", "watch")

CANONICAL = "https://www.youtube.com/watch?v={id}"


def _host_ok(host: str) -> bool:
    host = (host or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host in YOUTUBE_HOSTS


def extract_url(text: str) -> str | None:
    """First ``http(s)`` URL in the text, with trailing punctuation removed."""
    match = URL_RE.search(text or "")
    if not match:
        return None
    return match.group(0).rstrip(".,;:!?")


def _video_id_from_parts(parsed) -> str | None:
    query = parse_qs(parsed.query)
    candidate = (query.get("v") or [""])[0]
    if VIDEO_ID_RE.match(candidate or ""):
        return candidate

    segments = [segment for segment in (parsed.path or "").split("/") if segment]
    if not segments:
        return None
    if segments[0] in ID_PATHS and len(segments) > 1 and VIDEO_ID_RE.match(segments[1]):
        return segments[1]
    if VIDEO_ID_RE.match(segments[0]):
        return segments[0]
    return None


def canonise(url: str) -> str | None:
    """Return the canonical watch URL for a YouTube link, or ``None``."""
    try:
        parsed = urlparse(url)
    except ValueError:
        return None
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    if not _host_ok(parsed.hostname or ""):
        return None
    video_id = _video_id_from_parts(parsed)
    return CANONICAL.format(id=video_id) if video_id else None


def youtube_link(text: str) -> tuple[str | None, str]:
    """``(canonical_url, reason)`` for a message.

    ``reason`` is empty when a link was found; otherwise it explains the refusal in
    user-facing language.
    """
    text = (text or "").strip()
    if not text:
        return None, ""

    url = extract_url(text)
    if url is not None:
        canonical = canonise(url)
        if canonical:
            return canonical, ""
        host = (urlparse(url).hostname or url).lower()
        if host.startswith("www."):
            host = host[4:]
        return None, f"{host} is not a YouTube link."

    # A bare video id, pasted on its own.
    if VIDEO_ID_RE.match(text):
        return CANONICAL.format(id=text), ""

    return None, ""


__all__ = ["CANONICAL", "VIDEO_ID_RE", "YOUTUBE_HOSTS", "canonise", "extract_url", "youtube_link"]
