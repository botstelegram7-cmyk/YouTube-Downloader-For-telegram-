"""Request management: one job per user, cancellation, cleanup."""

from __future__ import annotations

import asyncio

from bot.jobs import JobManager
from bot.qualities import build_options, find_option
from bot.utils import make_temp_dir
from config import load_settings

SETTINGS = load_settings({"BOT_TOKEN": "123456789:" + "A" * 35, "DOWNLOAD_DIR": "/tmp/ytbot-test-jobs"})

INFO = {
    "title": "Test video",
    "duration": 60,
    "formats": [
        {"ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 720, "filesize": 10_000_000},
        {"ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 360, "filesize": 4_000_000},
        {"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 1_000_000},
    ],
}
OPTIONS = build_options(INFO, SETTINGS)


class FakeMessage:
    def __init__(self, message_id: int = 1, text: str = "") -> None:
        self.message_id = message_id
        self.text = text


class FakeBot:
    """Records everything sent to Telegram (messages and status edits)."""

    def __init__(self) -> None:
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.deleted: list[int] = []
        self._next = 10

    async def send_message(self, chat_id: int, text: str, **kwargs) -> FakeMessage:
        self.sent.append(text)
        self._next += 1
        return FakeMessage(self._next, text)

    async def edit_message_text(self, **kwargs) -> FakeMessage:
        self.edits.append(kwargs["text"])
        return FakeMessage(kwargs["message_id"], kwargs["text"])

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        self.deleted.append(message_id)
        return True


def all_texts(bot: FakeBot) -> str:
    return "\n".join(bot.sent + bot.edits)


def make_manager(monkeypatch, *, hold: float = 30.0) -> tuple[JobManager, FakeBot, dict]:
    bot = FakeBot()
    manager = JobManager(SETTINGS, bot)  # type: ignore[arg-type]
    state = {"started": 0, "dirs": []}

    async def fake_execute(job) -> None:
        job.temp_dir = make_temp_dir(SETTINGS.download_dir, prefix=f"test-{job.id}-")
        state["dirs"].append(job.temp_dir)
        state["started"] += 1
        try:
            await asyncio.sleep(hold)
        except asyncio.CancelledError:
            raise

    monkeypatch.setattr(manager, "_execute", fake_execute)
    return manager, bot, state


async def start(manager: JobManager, *, user_id: int = 1, key: str = "720p", message_id: int = 5):
    return await manager.start_job(
        chat_id=user_id,
        user_id=user_id,
        url="https://example.com/video",
        option=find_option(OPTIONS, key),
        title="Test video",
        token="tok12345",
        reply_to_message_id=1,
        status_message=FakeMessage(message_id),
    )


async def test_a_new_request_cancels_the_previous_one(monkeypatch):
    manager, bot, state = make_manager(monkeypatch)
    first = await start(manager)
    await asyncio.sleep(0.1)
    assert state["started"] == 1

    second = await start(manager, key="360p", message_id=7)
    await asyncio.sleep(0.1)

    assert first.task.done(), "the previous job must be stopped"
    assert manager.for_user(1).id == second.id
    assert "previous request" in all_texts(bot)
    assert not state["dirs"][0].exists(), "the cancelled job's files must be deleted"

    await manager.cancel_all()


async def test_cancel_user_stops_the_job_and_cleans_up(monkeypatch):
    manager, bot, state = make_manager(monkeypatch)
    await start(manager)
    await asyncio.sleep(0.1)

    stopped = await manager.cancel_user(1)

    assert stopped == 1
    assert manager.for_user(1) is None
    assert "Cancelled" in all_texts(bot)
    assert not state["dirs"][0].exists()
    assert manager.cancelled == 1


async def test_cancel_user_is_a_no_op_without_a_job(monkeypatch):
    manager, _bot, _state = make_manager(monkeypatch)
    assert await manager.cancel_user(42) == 0


async def test_cancel_all_stops_every_user(monkeypatch):
    manager, _bot, _state = make_manager(monkeypatch)
    await start(manager, user_id=1)
    await start(manager, user_id=2, message_id=8)
    await asyncio.sleep(0.1)

    stopped = await manager.cancel_all()

    assert stopped == 2
    assert manager.active == 0


async def test_jobs_are_isolated_per_user(monkeypatch):
    manager, _bot, _state = make_manager(monkeypatch)
    job_one = await start(manager, user_id=1)
    job_two = await start(manager, user_id=2, message_id=8)
    await asyncio.sleep(0.1)

    assert manager.for_user(1).id == job_one.id
    assert manager.for_user(2).id == job_two.id
    assert manager.active == 2
    snapshot = manager.snapshot()
    assert {entry["user_id"] for entry in snapshot} == {1, 2}

    await manager.cancel_all()


async def test_stats_count_outcomes(monkeypatch):
    manager, _bot, _state = make_manager(monkeypatch, hold=0.05)
    await start(manager)
    await asyncio.sleep(0.3)

    stats = manager.stats(SETTINGS)
    assert stats["completed"] == 1
    assert stats["active"] == 0


async def test_a_cancelled_job_does_not_leave_its_directory(monkeypatch):
    manager, _bot, state = make_manager(monkeypatch)
    await start(manager)
    await asyncio.sleep(0.1)
    directory = state["dirs"][0]
    assert directory.exists()

    await manager.cancel_user(1)

    assert not directory.exists()
