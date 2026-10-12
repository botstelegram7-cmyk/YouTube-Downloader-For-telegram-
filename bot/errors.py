"""Exception hierarchy. Every user-visible failure maps to one of these."""

from __future__ import annotations


class BotError(Exception):
    """Base class for expected, user-reportable failures."""

    #: Short, human-readable explanation shown in Telegram.
    reason: str = "Something went wrong."

    def __init__(self, reason: str | None = None) -> None:
        self.reason = reason or self.reason
        super().__init__(self.reason)

    def user_message(self) -> str:
        return self.reason


class ConfigProblem(BotError):
    reason = "The bot is misconfigured. The server log has the details."


class ProbeError(BotError):
    reason = "The link could not be read."


class NoFormatsError(BotError):
    reason = "No downloadable format was found for that link."


class DownloadError(BotError):
    reason = "The download failed."


class DownloadCancelled(BotError):
    reason = "Cancelled."


class UploadError(BotError):
    reason = "The upload to Telegram failed."


class UploadCancelled(BotError):
    reason = "Cancelled during upload."


class TooLargeError(BotError):
    """Raised when the produced file cannot be sent through the current Bot API."""

    def __init__(self, size_bytes: int, limit_mb: int) -> None:
        self.size_bytes = size_bytes
        self.limit_mb = limit_mb
        super().__init__(f"The file is {size_bytes / 1024 / 1024:.0f} MB, above the {limit_mb} MB limit.")


class CommandError(BotError):
    """An external command (ffmpeg, ffprobe) failed."""

    def __init__(self, command: str, detail: str = "", *, timed_out: bool = False) -> None:
        self.command = command
        self.detail = detail
        self.timed_out = timed_out
        super().__init__(f"{command} timed out." if timed_out else f"{command} failed: {detail or 'unknown error'}")


class MissingDependency(BotError):
    reason = "A required program is missing on the server."
