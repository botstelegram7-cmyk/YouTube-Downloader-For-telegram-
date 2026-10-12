"""Short-lived cache for links that are waiting for the user to choose something.

A quality picker keeps the resolved YouTube track here; a torrent picker keeps the torrent's
file list and the downloaded ``.torrent`` itself; a direct link keeps the URL and what the
server said about it. Buttons carry an opaque token, never an index the user could edit into
something else, and every lookup is scoped to the user who made the request.
"""

from __future__ import annotations

import logging
import secrets
import time
from dataclasses import dataclass, field
from pathlib import Path

from bot.sources import Source

log = logging.getLogger("bot.cache")

DEFAULT_TTL_SECONDS = 3600
DEFAULT_MAX_ENTRIES = 200


@dataclass
class PendingFetch:
    """A torrent or direct link waiting for the user's choice."""

    kind: str  # "torrent" | "direct"
    source: Source
    user_id: int = 0
    label: str = ""
    title: str = ""
    total_size: int = 0
    files: list[tuple[int, str, int]] = field(default_factory=list)
    torrent_path: Path | None = None
    detail: str = ""
    created: float = field(default_factory=time.time)

    def file_at(self, index: int) -> tuple[int, str, int] | None:
        return next((item for item in self.files if item[0] == index), None)

    def describe(self) -> str:
        return self.title or self.label or self.source.url[:60]


@dataclass
class CachedSource:
    """A link kept only so a failed job can offer "try again"."""

    source: Source
    user_id: int = 0
    created: float = field(default_factory=time.time)


class SourceCache:
    """Token → :class:`CachedSource`. The same TTL rules as :class:`FetchCache`."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[str, CachedSource] = {}

    def put(self, source: Source, *, user_id: int = 0) -> str:
        token = secrets.token_urlsafe(9)
        self._items[token] = CachedSource(source=source, user_id=user_id)
        self._evict()
        return token

    def get(self, token: str, *, user_id: int | None = None) -> Source | None:
        cutoff = time.time() - self.ttl_seconds
        self._items = {key: item for key, item in self._items.items() if item.created >= cutoff}
        entry = self._items.get(token or "")
        if entry is None:
            return None
        if user_id is not None and entry.user_id and int(user_id) != int(entry.user_id):
            return None
        return entry.source

    def _evict(self) -> None:
        while len(self._items) > self.max_entries:
            oldest = min(self._items.items(), key=lambda pair: pair[1].created)[0]
            self._items.pop(oldest, None)

    def __len__(self) -> int:
        return len(self._items)


class FetchCache:
    """Token → :class:`PendingFetch`, with a TTL and a hard entry limit."""

    def __init__(self, ttl_seconds: float = DEFAULT_TTL_SECONDS, max_entries: int = DEFAULT_MAX_ENTRIES) -> None:
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self._items: dict[str, PendingFetch] = {}

    def put(self, entry: PendingFetch) -> str:
        token = secrets.token_urlsafe(9)
        entry.created = time.time()
        self._items[token] = entry
        self._evict()
        return token

    def get(self, token: str, *, user_id: int | None = None) -> PendingFetch | None:
        self._prune()
        entry = self._items.get(token or "")
        if entry is None:
            return None
        if user_id is not None and entry.user_id and int(user_id) != int(entry.user_id):
            log.warning("Token reuse attempt by user %s", user_id)
            return None
        return entry

    def drop(self, token: str) -> None:
        self._items.pop(token or "", None)

    def drop_for_user(self, user_id: int) -> int:
        """Forget everything a user was still choosing from."""
        tokens = [token for token, entry in self._items.items() if entry.user_id == user_id]
        for token in tokens:
            self._items.pop(token, None)
        return len(tokens)

    # -- housekeeping ------------------------------------------------------- #
    def _prune(self) -> None:
        cutoff = time.time() - self.ttl_seconds
        for token in [token for token, entry in self._items.items() if entry.created < cutoff]:
            self._items.pop(token, None)

    def _evict(self) -> None:
        self._prune()
        while len(self._items) > self.max_entries:
            oldest = min(self._items.items(), key=lambda pair: pair[1].created)[0]
            self._items.pop(oldest, None)

    def __len__(self) -> int:
        self._prune()
        return len(self._items)


__all__ = [
    "DEFAULT_MAX_ENTRIES",
    "DEFAULT_TTL_SECONDS",
    "CachedSource",
    "FetchCache",
    "PendingFetch",
    "SourceCache",
]
