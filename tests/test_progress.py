"""Status editing: throttling, finalisation and upload-progress accounting."""

from __future__ import annotations

import asyncio

from bot.progress import StatusReporter, UploadProgressFile


class FakeMessage:
    message_id = 42
    text = "start"


class FakeBot:
    def __init__(self, fail_with=None) -> None:
        self.edits: list[str] = []
        self.deleted = False
        self._fail_with = fail_with

    async def edit_message_text(self, **kwargs):
        if self._fail_with is not None:
            raise self._fail_with
        self.edits.append(kwargs["text"])

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted = True


async def test_edits_are_throttled_by_the_interval():
    bot = FakeBot()
    reporter = StatusReporter(bot, 1, FakeMessage(), interval=10.0)  # type: ignore[arg-type]

    await reporter.update("one")
    await reporter.update("two")  # same second: suppressed
    await reporter.update("three", force=True)

    assert bot.edits == ["one", "three"]


async def test_identical_text_is_never_resent():
    bot = FakeBot()
    reporter = StatusReporter(bot, 1, FakeMessage(), interval=0.5)  # type: ignore[arg-type]

    await reporter.update("same", force=True)
    await reporter.update("same", force=True)

    assert bot.edits == ["same"]


async def test_finish_closes_the_reporter():
    bot = FakeBot()
    reporter = StatusReporter(bot, 1, FakeMessage(), interval=0.5)  # type: ignore[arg-type]

    await reporter.finish("done")
    await reporter.finish("again")
    await reporter.update("late", force=True)

    assert bot.edits == ["done"]


async def test_delete_marks_the_reporter_finished():
    bot = FakeBot()
    reporter = StatusReporter(bot, 1, FakeMessage(), interval=0.5)  # type: ignore[arg-type]

    await reporter.delete()
    await reporter.update("late", force=True)

    assert bot.deleted is True
    assert bot.edits == []


async def test_edit_failures_never_propagate():
    from telegram.error import BadRequest

    bot = FakeBot(fail_with=BadRequest("message is not modified"))
    reporter = StatusReporter(bot, 1, FakeMessage(), interval=0.1)  # type: ignore[arg-type]

    await reporter.update("hello")  # must not raise
    await reporter.finish("bye")


async def test_updates_from_another_thread_are_scheduled_on_the_loop():
    bot = FakeBot()
    reporter = StatusReporter(bot, 1, FakeMessage(), interval=0.0)  # type: ignore[arg-type]

    reporter.update_from_thread("from the download thread")
    await asyncio.sleep(0.1)

    assert bot.edits == ["from the download thread"]


def test_upload_progress_counts_real_bytes_read(tmp_path):
    payload = tmp_path / "file.bin"
    payload.write_bytes(b"x" * 1000)
    seen: list[tuple[int, int]] = []

    with UploadProgressFile(payload, lambda sent, total: seen.append((sent, total))) as reader:
        assert reader.total == 1000
        chunk = reader.read(400)
        assert chunk == b"x" * 400
        assert reader.sent == 400
        rest = reader.read()
        assert len(rest) == 600
        assert reader.sent == 1000

    assert seen[0] == (400, 1000)
    assert seen[-1] == (1000, 1000)
    assert reader.percent == 100.0


def test_upload_progress_supports_seeking_for_retries(tmp_path):
    payload = tmp_path / "file.bin"
    payload.write_bytes(b"y" * 500)
    reader = UploadProgressFile(payload, lambda *_: None)
    reader.read(100)
    assert reader.sent == 100

    reader.seek(0)
    assert reader.sent == 0, "rewinding restarts the counter"
    reader.read(100)

    assert reader.sent == 100
    assert reader.total == 500


def test_a_failing_progress_callback_does_not_break_the_read(tmp_path):
    payload = tmp_path / "file.bin"
    payload.write_bytes(b"z" * 10)

    def explode(sent: int, total: int) -> None:
        raise RuntimeError("boom")

    with UploadProgressFile(payload, explode) as reader:
        assert reader.read() == b"z" * 10
