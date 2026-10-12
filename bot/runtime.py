"""Shared runtime state, stored in ``context.bot_data`` so handlers can reach it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from telegram.ext import ContextTypes

from bot.access import Access
from bot.assets import AssetLibrary
from bot.jobs import JobManager
from bot.typetube import TrackCache
from bot.utils import utc_now
from config import Settings

BOT_DATA_KEY = "runtime"


@dataclass
class Runtime:
    """Everything the handlers need that outlives a single update."""

    settings: Settings
    manager: JobManager
    tracks: TrackCache = field(default_factory=TrackCache)
    uploader: object | None = None
    assets: AssetLibrary | None = None
    access: Access = None  # type: ignore[assignment]
    bot_username: str = ""
    started_at: datetime = field(default_factory=utc_now)
    background_tasks: set = field(default_factory=set)

    @property
    def owner_label(self) -> str:
        """How the owner is credited to users: ``@username``, the numeric id, or nothing.

        The id form is used when only ``OWNER_USER_ID`` is set, and an unconfigured owner
        is credited with nothing at all — users never see a placeholder like "not configured".
        """
        if self.settings.owner_username:
            return f"@{self.settings.owner_username}"
        owner_id = self.access.owner_id if self.access is not None else 0
        return str(owner_id or self.settings.owner_user_id or "")

    def track(self, task) -> None:
        """Keep a reference to a fire-and-forget task (and drop it when done)."""
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)


def runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime:
    return context.bot_data[BOT_DATA_KEY]
