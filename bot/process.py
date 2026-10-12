"""Tracked subprocess execution.

Every external command (ffmpeg, ffprobe) runs through :func:`run_command` so that:

* a cancel request terminates the process instead of leaving it running,
* a timeout cannot hang a job forever,
* the job always knows which processes it started, and can reap them.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
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
) -> CommandResult:
    """Run ``command`` to completion, honouring cancellation and timeouts."""
    name = label or os.path.basename(command[0]) if command else "command"
    log.debug("Running %s: %s", name, " ".join(command[:8]))

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
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
            with __import__("contextlib").suppress(asyncio.CancelledError):
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


def which(program: str) -> str | None:
    return shutil.which(program)
