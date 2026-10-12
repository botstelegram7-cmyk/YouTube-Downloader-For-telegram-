"""Shared runtime state, stored in ``context.bot_data`` so handlers can reach it."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from telegram.ext import ContextTypes

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
    started_at: datetime = field(default_factory=utc_now)
    background_tasks: set = field(default_factory=set)

    def track(self, task) -> None:
        """Keep a reference to a fire-and-forget task (and drop it when done)."""
        self.background_tasks.add(task)
        task.add_done_callback(self.background_tasks.discard)


def runtime(context: ContextTypes.DEFAULT_TYPE) -> Runtime:
    return context.bot_data[BOT_DATA_KEY]
