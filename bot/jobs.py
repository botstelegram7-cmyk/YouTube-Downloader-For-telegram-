"""Job lifecycle: one active job per user, cancellation, progress and cleanup.

Rules this module enforces:

* A user has at most **one** job. A new request cancels the previous one — its
  processes are killed, its files are deleted, and its status message is closed.
* ``/cancel`` stops the download, the conversion and the upload for that user.
* Every job owns a temporary directory that is removed when it ends, whatever
  the outcome.
* Cancellation reaches the TypeTube CLI and its range workers through the job's
  process registry (SIGTERM, then SIGKILL), and every extraction is wrapped in a timeout.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from telegram import Bot
from telegram.error import TelegramError

from bot import texts
from bot.cache import SourceCache
from bot.download import DownloadResult, ProgressUpdate, download_from_source, download_media
from bot.errors import (
    BotError,
    DiskSpaceError,
    DownloadCancelled,
    DownloadError,
    MissingDependency,
    ProbeError,
    TooLargeError,
    UploadCancelled,
    UploadError,
)
from bot.keyboards import cancel_keyboard, retry_keyboard, retry_source_keyboard
from bot.process import ProcessRegistry
from bot.progress import ProgressCard
from bot.qualities import QualityOption, build_options, smaller_video_options
from bot.sources import Source
from bot.texts import cancelled as cancelled_text
from bot.typetube import Track
from bot.upload import send_media
from bot.utils import (
    disk_free_bytes,
    human_duration,
    human_eta,
    human_rate,
    human_size,
    make_temp_dir,
    progress_bar,
    remove_dir,
    utc_now,
)
from config import Settings

log = logging.getLogger("bot.jobs")

PHASE_ICONS = {
    "queued": "🕒",
    "download": "⬇️",
    "convert": "🎛",
    "upload": "⬆️",
}


@dataclass
class Request:
    """What the user asked for: a YouTube quality, a file inside a torrent, or a direct URL."""

    chat_id: int
    user_id: int
    url: str
    title: str
    option: QualityOption | None = None
    track: Track | None = None
    token: str = ""
    source: Source | None = None
    torrent_path: Path | None = None
    torrent_index: int = 0

    @classmethod
    def youtube(cls, **kwargs) -> Request:
        return cls(**kwargs)


@dataclass
class Job:
    """One download request, from the button press to the delivered file."""

    user_id: int
    chat_id: int
    url: str
    title: str
    option: QualityOption | None = None
    source: Source | None = None
    source_token: str = ""
    torrent_path: Path | None = None
    torrent_index: int = 0
    token: str = ""
    reply_to_message_id: int | None = None
    track: Track | None = None
    status: ProgressCard | None = None
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    registry: ProcessRegistry = field(default_factory=ProcessRegistry)
    temp_dir: Path | None = None
    phase: str = "queued"
    started_at: datetime = field(default_factory=utc_now)
    superseded: bool = False
    result: DownloadResult | None = None
    error: str = ""
    task: asyncio.Task | None = None

    @property
    def quality_label(self) -> str:
        if self.option is not None:
            return self.option.label
        if self.source is not None:
            return "🧲 Torrent" if self.source.is_torrent else "📥 Direct"
        return "Download"

    @property
    def age_seconds(self) -> float:
        return (utc_now() - self.started_at).total_seconds()

    def describe(self) -> str:
        icon = PHASE_ICONS.get(self.phase, "⏳")
        return f"{icon} {self.quality_label} · {self.title[:48]} ({human_duration(self.age_seconds)})"

    def is_active(self) -> bool:
        return self.task is not None and not self.task.done()


class JobManager:
    """Owns every running job: start, cancel, report, clean up."""

    def __init__(self, settings: Settings, bot: Bot, uploader=None, assets=None) -> None:
        self.settings = settings
        self.bot = bot
        self.uploader = uploader
        self.assets = assets
        #: Links kept so a failed torrent/direct job can offer "try again".
        self.sources = SourceCache()
        #: How often a status message may be edited. Telegram allows roughly one message per
        #: second per chat; the configured floor (5 s by default) keeps a busy bot well clear
        #: of that and was an explicit requirement.
        self.progress_interval = max(settings.progress_interval, settings.progress_min_interval)
        self._jobs: dict[int, Job] = {}
        self._lock = asyncio.Lock()
        self._slots = asyncio.Semaphore(max(1, settings.max_concurrent_jobs))
        self.completed = 0
        self.failed = 0
        self.cancelled = 0

    # -- introspection ------------------------------------------------------
    @property
    def active(self) -> int:
        return sum(1 for job in self._jobs.values() if job.is_active())

    def for_user(self, user_id: int) -> Job | None:
        job = self._jobs.get(user_id)
        return job if job and job.is_active() else None

    def snapshot(self) -> list[dict]:
        return [
            {
                "user_id": job.user_id,
                "job": job.id,
                "phase": job.phase,
                "title": job.title[:60],
                "quality": job.quality_label,
                "running_for": round(job.age_seconds, 1),
            }
            for job in self._jobs.values()
            if job.is_active()
        ]

    def stats(self, settings: Settings) -> dict:
        return {
            "active": self.active,
            "slots": settings.max_concurrent_jobs,
            "completed": self.completed,
            "failed": self.failed,
            "cancelled": self.cancelled,
            "free_disk": human_size(disk_free_bytes(settings.download_dir)),
        }

    # -- starting -----------------------------------------------------------
    async def start_job(
        self,
        request: Request,
        *,
        reply_to_message_id: int | None = None,
        status_message=None,
    ) -> Job:
        """Cancel the user's previous job (if any) and start this one."""
        user_id = request.user_id
        async with self._lock:
            previous = self._jobs.get(user_id)
            if previous is not None and previous.is_active():
                await self._cancel(previous, reason="replaced")
                self.cancelled += 1

            job = Job(
                user_id=user_id,
                chat_id=request.chat_id,
                url=request.url,
                option=request.option,
                title=request.title,
                token=request.token,
                track=request.track,
                source=request.source,
                source_token=(
                    self.sources.put(request.source, user_id=user_id) if request.source is not None else ""
                ),
                torrent_path=request.torrent_path,
                torrent_index=request.torrent_index,
                reply_to_message_id=reply_to_message_id,
            )
            if status_message is not None:
                job.status = ProgressCard(
                    self.bot,
                    request.chat_id,
                    status_message,
                    interval=self.progress_interval,
                    loop=asyncio.get_running_loop(),
                )
            self._jobs[user_id] = job

        job.task = asyncio.create_task(self._run(job), name=f"job-{job.id}")
        log.info("Job %s started for user %s (%s)", job.id, user_id, job.quality_label)
        return job

    # -- cancelling ---------------------------------------------------------
    async def cancel_user(self, user_id: int) -> int:
        """Stop everything this user has running. Returns how many jobs stopped."""
        async with self._lock:
            job = self._jobs.get(user_id)
            if job is None or not job.is_active():
                return 0
            await self._cancel(job, reason="user")
            self.cancelled += 1
            return 1

    async def cancel_all(self) -> int:
        async with self._lock:
            jobs = [job for job in self._jobs.values() if job.is_active()]
            for job in jobs:
                await self._cancel(job, reason="shutdown")
            self.cancelled += len(jobs)
            return len(jobs)

    async def _cancel(self, job: Job, *, reason: str) -> None:
        """Kill processes, drop files and close the status message."""
        job.superseded = True
        job.cancel_event.set()

        await job.registry.terminate_all()

        if job.task is not None and not job.task.done():
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, asyncio.TimeoutError, Exception):
                await asyncio.wait_for(job.task, timeout=10)

        remove_dir(job.temp_dir)
        job.temp_dir = None

        if job.status is not None:
            if reason == "replaced":
                await job.status.finish("♻️ Cancelled your previous request to start the new one.")
            elif reason == "user":
                await job.status.finish(cancelled_text())
            else:
                await job.status.delete()

        self._jobs.pop(job.user_id, None)
        log.info("Job %s cancelled (%s)", job.id, reason)

    # -- the pipeline -------------------------------------------------------
    async def _run(self, job: Job) -> None:
        try:
            await self._execute(job)
            self.completed += 1
        except asyncio.CancelledError:
            # _cancel() has already cleaned up and closed the status message.
            log.info("Job %s stopped", job.id)
        except (DownloadCancelled, UploadCancelled):
            self.cancelled += 1
            await self._finish_error(job, cancelled_text())
        except TooLargeError as exc:
            self.failed += 1
            log.warning("Job %s failed: file too large for this transport (%s)", job.id, human_size(exc.size_bytes))
            await self._finish_too_large(job, exc)
        except DiskSpaceError as exc:
            self.failed += 1
            log.warning("Job %s failed: %s", job.id, exc.reason)
            await self._finish_error(job, texts.job_failed(exc.reason))
        except (DownloadError, ProbeError, UploadError, MissingDependency) as exc:
            self.failed += 1
            # Every expected failure is named in the log: a silent failure is undebuggable.
            log.warning("Job %s failed (%s): %s", job.id, type(exc).__name__, exc.reason)
            await self._finish_error(job, texts.job_failed(exc.reason))
        except BotError as exc:
            self.failed += 1
            log.warning("Job %s failed (%s): %s", job.id, type(exc).__name__, exc.reason)
            await self._finish_error(job, texts.job_failed(exc.reason))
        except Exception:
            self.failed += 1
            log.exception("Unexpected failure in job %s", job.id)
            await self._finish_error(
                job, texts.job_failed("An unexpected error occurred. The server log has the details.")
            )
        finally:
            if not job.superseded:
                await self._cleanup(job)
                self._jobs.pop(job.user_id, None)

    async def _open_card(self, job: Job) -> ProgressCard:
        """Send the status card: an animated one when rich messages are on, else text.

        The card is created *as media* when animation is enabled, because Telegram can only
        replace the media of a message that already has some — a text message can never
        become a GIF later.
        """
        loop = asyncio.get_running_loop()
        markup = cancel_keyboard(job.id)
        caption = texts.job_started(job.title, job.quality_label)

        if self.settings.rich_messages and self.assets is not None:
            asset = self.assets.animation("progress")
            if asset is not None:
                from telegram import InputFile

                animation = self.assets.file_id(asset.name) or InputFile(
                    asset.bytes(), filename=asset.name
                )
                try:
                    message = await self.bot.send_animation(
                        chat_id=job.chat_id,
                        animation=animation,
                        caption=caption,
                        parse_mode="HTML",
                        reply_markup=markup,
                    )
                    card = ProgressCard(
                        self.bot,
                        job.chat_id,
                        message,
                        interval=self.progress_interval,
                        loop=loop,
                        keyboard=markup,
                        animated=True,
                    )
                    card.assets = self.assets
                    card.remember_media(asset.name, message)
                    return card
                except TelegramError as exc:
                    # Telegram would not take the animation: a text card is still a good card.
                    log.warning("Animated status card unavailable (%s); using text", exc)

        message = await self.bot.send_message(
            chat_id=job.chat_id,
            text=caption,
            parse_mode="HTML",
            disable_web_page_preview=True,
            reply_markup=markup,
        )
        return ProgressCard(
            self.bot,
            job.chat_id,
            message,
            interval=self.progress_interval,
            loop=loop,
            keyboard=markup,
        )

    async def _execute(self, job: Job) -> None:
        settings = self.settings

        if job.status is None:
            try:
                job.status = await self._open_card(job)
            except Exception as exc:
                # No status card is survivable; a dead job with no explanation is not.
                log.warning("Could not open a status card for job %s: %s", job.id, exc)
                job.status = None

        job.temp_dir = make_temp_dir(settings.download_dir, prefix=f"job-{job.id}-")

        async with self._slots:
            job.phase = "download"
            if self.assets is not None and job.status is not None:
                job.status.show_animation(
                    self.assets.animation("progress"),
                    texts.card_caption(job.title, job.quality_label, "download"),
                    key="download",
                )
            result = await self._fetch(job)

            job.phase = "upload"
            if self.assets is not None and job.status is not None:
                job.status.show_animation(
                    self.assets.animation("upload"),
                    texts.card_caption(job.title, job.quality_label, "upload"),
                    key="upload",
                )
            outcome = await send_media(
                bot=self.bot,
                chat_id=job.chat_id,
                result=result,
                settings=settings,
                reporter=job.status,
                reply_to_message_id=job.reply_to_message_id,
                source_url=job.url,
                uploader=self.uploader,
                cancel_event=job.cancel_event,
            )
            job.result = result
            log.info(
                "Job %s delivered via %s (%s, %s, faststart=%s, thumbnail=%s)",
                job.id,
                outcome.method,
                human_size(result.size),
                result.resolution or result.quality_label,
                result.faststart,
                outcome.thumbnail_attached,
            )

        # The file speaks for itself; the status message has done its job.
        await job.status.delete()

    async def _fetch(self, job: Job) -> DownloadResult:
        """Download from whichever source this job came from."""
        if job.source is not None and not job.source.is_youtube:
            return await self._source_phase(job)
        return await self._download_phase(job)

    async def _source_phase(self, job: Job) -> DownloadResult:
        """Torrent and direct-link jobs: fetch, then verify like any other download."""
        progress = self._progress_callback(job)

        async def run() -> DownloadResult:
            return await download_from_source(
                source=job.source,  # type: ignore[arg-type]
                dest_dir=job.temp_dir,  # type: ignore[arg-type]
                settings=self.settings,
                on_progress=progress,
                cancel_event=job.cancel_event,
                registry=job.registry,
                torrent_path=job.torrent_path,
                torrent_index=job.torrent_index,
            )

        try:
            return await asyncio.wait_for(run(), timeout=self.settings.download_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise DownloadError(
                f"That download took longer than {human_duration(self.settings.download_timeout_seconds)} "
                "and was stopped."
            ) from exc

    async def _download_phase(self, job: Job) -> DownloadResult:
        progress = self._progress_callback(job)

        async def run() -> DownloadResult:
            return await download_media(
                url=job.url,
                option=job.option,
                track=job.track,
                dest_dir=job.temp_dir,
                settings=self.settings,
                on_progress=progress,
                cancel_event=job.cancel_event,
                registry=job.registry,
            )

        try:
            return await asyncio.wait_for(run(), timeout=self.settings.download_timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise DownloadError(
                f"That download took longer than {human_duration(self.settings.download_timeout_seconds)} "
                "and was stopped. Try a lower quality."
            ) from exc

    def _progress_callback(self, job: Job):
        """Render progress updates; called from the CLI's output stream."""
        state = {"started": time.monotonic(), "phase": job.phase, "last": 0.0, "numbers": False}

        # Telegram allows roughly one message per second per chat, so edits are spaced by
        # the larger of the configured interval and the enforced floor (5 s by default).
        interval = self.progress_interval

        def callback(update: ProgressUpdate) -> None:
            job.phase = update.phase
            if job.status is None:
                return
            now = time.monotonic()
            new_phase = update.phase != state["phase"]
            # The first sample that carries real numbers is shown immediately — otherwise a
            # five-second floor would hide the speed and the ETA until the job is finished.
            first_numbers = not state.get("numbers") and bool(update.total or update.percent)
            # Throttle here too: hooks fire many times per second, and Telegram allows about
            # one message per second per chat.
            if not new_phase and not first_numbers and now - state["last"] < interval:
                return
            if first_numbers:
                state["numbers"] = True
            state["last"] = now
            state["phase"] = update.phase
            job.status.update_from_thread(self._render(job, update), force=first_numbers)

        return callback

    @staticmethod
    def _live_line(update: ProgressUpdate) -> str:
        """Speed and ETA, each only when it is known — no invented numbers.

        The engine reports a speed for the range it is fetching; the ETA is only printed
        when there is a total size to divide, because otherwise it would be a guess dressed
        up as a promise.
        """
        parts = []
        if update.speed:
            parts.append(f"⚡ {human_rate(update.speed)}")
        if update.eta is not None and update.total:
            parts.append(f"⏳ ETA {human_eta(update.eta)}")
        return "  ·  ".join(parts)

    def _render(self, job: Job, update: ProgressUpdate) -> str:
        if update.phase == "upload":
            title = "⬆️ <b>Uploading to Telegram</b>"
        elif update.phase == "convert":
            title = "🎛 <b>Converting</b>"
        else:
            title = "⬇️ <b>Downloading</b>"

        lines = [f"{title} — {job.quality_label}", f"<i>{job.title}</i>", ""]

        if update.phase == "convert":
            lines.append(update.note or "Muxing, adding the index for streaming…")
            return "\n".join(lines)

        percent = update.percent
        if percent is None:
            lines.append(f"{progress_bar(0)}  <b>{human_size(update.downloaded)}</b>")
        else:
            lines.append(f"{progress_bar(percent)}  <b>{percent:.1f}%</b>")
            total = f" / {human_size(update.total)}" if update.total else ""
            lines.append(f"📦 {human_size(update.downloaded)}{total}")
            lines.append(self._live_line(update))
        if update.note:
            lines.append(f"<i>{update.note[:120]}</i>")
        return "\n".join(lines)

    # -- ending -------------------------------------------------------------
    async def _finish_error(self, job: Job, text: str) -> None:
        """Close the status message, offering smaller qualities when that helps."""
        job.error = text
        if job.superseded or job.status is None:
            return

        reply_markup = None
        if job.track is not None and job.option is not None:
            options = build_options(job.track, self.settings, capability_bytes=self.settings.upload_capability_bytes)
            smaller = smaller_video_options(options, job.option)
            if smaller and job.token:
                reply_markup = retry_keyboard(smaller, job.token)
        if reply_markup is None and job.source is not None and job.source_token:
            # Torrents and direct links have nothing to "lower": offer to read the link again.
            reply_markup = retry_source_keyboard(job.source_token)

        await job.status.finish(text, reply_markup=reply_markup)

    async def _finish_too_large(self, job: Job, error: TooLargeError) -> None:
        text = texts.too_large(error.size_bytes / 1024 / 1024, self.settings.summary())
        await self._finish_error(job, text)

    async def _cleanup(self, job: Job) -> None:
        if os.getenv("KEEP_FAILED_FILES", "").lower() in ("1", "true", "yes") and job.error:
            log.info("Keeping files for failed job %s at %s", job.id, job.temp_dir)
            job.temp_dir = None
            return
        remove_dir(job.temp_dir)
        job.temp_dir = None
