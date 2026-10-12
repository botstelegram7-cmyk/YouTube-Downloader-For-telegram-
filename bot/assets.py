"""The bot's pictures and animations, and how they reach Telegram quickly.

Telegram never needs the same file twice: the first upload returns a ``file_id``, and every
later send can reference it by that id — no bytes travel again. The ids are cached in a small
JSON file next to the workspace, and an asset is only uploaded when its id is missing (a fresh
container, or a re-rendered image whose content changed).

If the cache file is not writable (a read-only root filesystem, an unmounted volume) the
module degrades to uploading from disk: slower, never broken.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("bot.assets")

#: Files the bot may send, with the Telegram type each one is sent as.
START_IMAGE = "start.jpg"
DOWNLOAD_ANIMATION = "progress.gif"
UPLOAD_ANIMATION = "upload.gif"

ANIMATIONS = {"progress": DOWNLOAD_ANIMATION, "upload": UPLOAD_ANIMATION}

ASSET_DIR = Path(__file__).resolve().parent.parent / "assets"


@dataclass(frozen=True)
class Asset:
    """One file on disk, with the Telegram id it is already known by (if any)."""

    name: str
    path: Path
    kind: str  # "photo" | "animation"

    @property
    def exists(self) -> bool:
        return self.path.is_file()

    @property
    def size(self) -> int:
        return self.path.stat().st_size if self.exists else 0

    def bytes(self) -> bytes:
        return self.path.read_bytes()


class AssetLibrary:
    """Finds the assets on disk and remembers their Telegram ``file_id``s."""

    def __init__(self, cache_path: Path | None = None, directory: Path | None = None) -> None:
        self.directory = Path(directory or ASSET_DIR)
        self.cache_path = Path(cache_path) if cache_path else self.directory / ".file_ids.json"
        self._ids: dict[str, str] = self._load()

    # -- disk -----------------------------------------------------------------
    def _load(self) -> dict[str, str]:
        try:
            data = json.loads(self.cache_path.read_text())
            return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._ids, indent=2, sort_keys=True))
        except OSError as exc:  # read-only filesystem: keep working, just re-upload
            log.debug("Could not persist the asset file_id cache: %s", exc)

    # -- lookup ---------------------------------------------------------------
    def get(self, name: str) -> Asset | None:
        asset = Asset(name=name, path=self.directory / name, kind="animation" if name.endswith(".gif") else "photo")
        return asset if asset.exists else None

    def animation(self, key: str) -> Asset | None:
        """``"progress"`` or ``"upload"`` → the animation asset, if it is installed."""
        name = ANIMATIONS.get(key)
        return self.get(name) if name else None

    def file_id(self, name: str) -> str | None:
        return self._ids.get(name)

    def remember(self, name: str, file_id: str | None) -> None:
        if file_id and self._ids.get(name) != file_id:
            self._ids[name] = file_id
            self._save()

    def describe(self) -> dict[str, str]:
        """What is installed, for ``/diag`` — names and sizes, no ids."""
        out: dict[str, str] = {}
        for name in (START_IMAGE, DOWNLOAD_ANIMATION, UPLOAD_ANIMATION):
            asset = self.get(name)
            if asset is None:
                out[name] = "missing"
            elif self.file_id(name):
                out[name] = f"{asset.size // 1024} KB, cached"
            else:
                out[name] = f"{asset.size // 1024} KB"
        return out
