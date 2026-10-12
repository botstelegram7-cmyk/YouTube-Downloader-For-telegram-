"""Tracked subprocess execution.

Every external command (the TypeTube CLI, ffmpeg, ffprobe) runs through
:func:`run_command` or :func:`run_streaming` so that:

* a cancel request terminates the process instead of leaving it running,
* a timeout cannot hang a job forever,
* the job always knows which processes it started, and can reap them.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass, field

from bot.errors import CommandError

log = logging.getLogger("bot.process")

KILL_GRACE_SECONDS = 5.0


@dataclass
class ProcessRegistry:
    """Processes belonging to one job."""

    _processes: set[asyncio.subprocess.Process] = field(default_factory=set)

    def add(self, process: asyncio.subprocess.Process) -> None:
        self._processes.add(process)

    def discard(self, process: asyncio.subprocess.Process) -> None:
        self._processes.discard(process)

    @property
    def active(self) -> int:
        return sum(1 for process in self._processes if process.returncode is None)

    async def terminate_all(self, grace: float = KILL_GRACE_SECONDS) -> None:
        """SIGTERM everything, then SIGKILL whatever is still alive."""
        alive = [process for process in self._processes if process.returncode is None]
        for process in alive:
            _signal(process, "terminate")
        if not alive:
            return
        await asyncio.sleep(0.2)
        still_alive = [process for process in alive if process.returncode is None]
        if still_alive:
            try:
                await asyncio.wait_for(asyncio.gather(*(process.wait() for process in still_alive)), timeout=grace)
            except (asyncio.TimeoutError, ProcessLookupError):
                for process in still_alive:
                    _signal(process, "kill")
        self._processes.clear()


def _signal(process: asyncio.subprocess.Process, action: str) -> None:
    try:
        if action == "terminate":
            process.terminate()
        else:
            process.kill()
    except (ProcessLookupError, RuntimeError):
        pass  # already gone


async def _terminate(process: asyncio.subprocess.Process) -> None:
    _signal(process, "terminate")
    try:
        await asyncio.wait_for(process.wait(), timeout=KILL_GRACE_SECONDS)
    except (asyncio.TimeoutError, ProcessLookupError):
        _signal(process, "kill")


async def _watch_cancel(cancel_event: asyncio.Event, process: asyncio.subprocess.Process) -> None:
    """Kill the process as soon as the job is cancelled."""
    await cancel_event.wait()
    if process.returncode is None:
        log.debug("Cancelling command (pid %s)", process.pid)
        await _terminate(process)


@dataclass
class CommandResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


async def run_command(
    command: list[str],
    *,
    timeout: float | None = None,
    registry: ProcessRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
    label: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """Run ``command`` to completion, honouring cancellation and timeouts."""
    name = label or os.path.basename(command[0]) if command else "command"
    log.debug("Running %s: %s", name, " ".join(command[:8]))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    if registry is not None:
        registry.add(process)

    watcher = asyncio.create_task(_watch_cancel(cancel_event, process)) if cancel_event is not None else None

    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
    except asyncio.TimeoutError as exc:
        await _terminate(process)
        raise CommandError(name, timed_out=True) from exc
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if registry is not None:
            registry.discard(process)

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError()

    return CommandResult(
        returncode=process.returncode or 0,
        stdout=(stdout or b"").decode("utf-8", "replace"),
        stderr=(stderr or b"").decode("utf-8", "replace"),
    )


@dataclass
class StreamResult:
    """Result of a streaming command: full output plus the segments as they arrived."""

    returncode: int
    stdout: str
    stderr: str
    segments: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _drain(text: str, buffer: list[str], emit) -> None:
    """Split ``text`` on newlines/carriage returns, handing complete segments to ``emit``."""
    buffer.append(text)
    pending = "".join(buffer)
    *complete, remainder = re.split(r"[\r\n]", pending)
    buffer.clear()
    if remainder:
        buffer.append(remainder)
    for segment in complete:
        if segment.strip():
            emit(segment)


async def _pump(stream: asyncio.StreamReader, sink: list[str], emit=None) -> None:
    """Read a pipe to EOF, keeping the full text and emitting complete lines live."""
    buffer: list[str] = []
    while True:
        chunk = await stream.read(8192)
        if not chunk:
            break
        text = chunk.decode("utf-8", "replace")
        sink.append(text)
        if emit is not None:
            _drain(text, buffer, emit)
    if emit is not None and buffer and buffer[0].strip():
        emit(buffer[0])


async def run_streaming(
    command: list[str],
    *,
    timeout: float | None = None,
    registry: ProcessRegistry | None = None,
    cancel_event: asyncio.Event | None = None,
    label: str | None = None,
    on_stdout: Callable[[str], None] | None = None,
    on_stderr: Callable[[str], None] | None = None,
    max_stderr_chars: int = 200_000,
    env: dict[str, str] | None = None,
) -> StreamResult:
    """Run ``command``, reporting its output as it arrives.

    Needed because the TypeTube CLI writes progress with carriage returns instead of
    newlines: without live parsing, a long download would look frozen for minutes.
    Cancellation and timeouts behave exactly like :func:`run_command`.
    """
    name = label or (os.path.basename(command[0]) if command else "command")
    log.debug("Running %s: %s", name, " ".join(command[:8]))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    if registry is not None:
        registry.add(process)

    watcher = asyncio.create_task(_watch_cancel(cancel_event, process)) if cancel_event is not None else None

    out_sink: list[str] = []
    err_sink: list[str] = []
    emitted: list[str] = []

    def emit(segment: str) -> None:
        emitted.append(segment)
        if on_stdout is not None:
            if len(emitted) > 5000:  # keep memory bounded on very chatty streams
                del emitted[:2500]
            on_stdout(segment)

    pumps = asyncio.gather(
        _pump(process.stdout, out_sink, emit),
        _pump(process.stderr, err_sink, on_stderr),
        return_exceptions=True,
    )

    try:
        await asyncio.wait_for(pumps, timeout=timeout)
        await process.wait()
    except asyncio.TimeoutError as exc:
        await _terminate(process)
        raise CommandError(name, timed_out=True) from exc
    finally:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if registry is not None:
            registry.discard(process)

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError()

    stderr = "".join(err_sink)
    if len(stderr) > max_stderr_chars:
        stderr = stderr[-max_stderr_chars:]
    return StreamResult(
        returncode=process.returncode or 0,
        stdout="".join(out_sink),
        stderr=stderr,
        segments=emitted,
    )


def which(program: str) -> str | None:
    return shutil.which(program)
