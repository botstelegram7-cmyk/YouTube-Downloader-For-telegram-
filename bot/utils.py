"""Small shared helpers: formatting, temp directories, escaping."""

from __future__ import annotations

import html
import os
import re
import shutil
import tempfile
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path

_UNITS = ("B", "KB", "MB", "GB", "TB")


def human_size(size: int | float | None) -> str:
    """``1536`` → ``1.5 KB``. Returns ``"unknown"`` for missing values."""
    if size is None:
        return "unknown"
    value = float(size)
    if value <= 0:
        return "0 B"
    for unit in _UNITS:
        if value < 1024 or unit == _UNITS[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"  # pragma: no cover - unreachable


def human_duration(seconds: int | float | None) -> str:
    """``3725`` → ``1:02:05``."""
    if not seconds or seconds <= 0:
        return "0:00"
    total = round(seconds)
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{secs:02d}"
    return f"{minutes}:{secs:02d}"


def human_rate(bytes_per_second: float | None) -> str:
    if not bytes_per_second or bytes_per_second <= 0:
        return "—"
    return f"{human_size(bytes_per_second)}/s"


def human_eta(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "—"
    return human_duration(seconds)


def progress_bar(percent: float, width: int = 12) -> str:
    """A text progress bar that looks right in a monospace Telegram message."""
    ratio = max(0.0, min(1.0, percent / 100))
    filled = round(ratio * width)
    return "▰" * filled + "▱" * (width - filled)


def safe_filename(name: str, *, max_length: int = 120, fallback: str = "media") -> str:
    """Sanitise a title for use as a filename (keeps unicode letters)."""
    cleaned = unicodedata.normalize("NFKC", name or "")
    cleaned = re.sub(r"[\x00-\x1f\x7f]", "", cleaned)
    cleaned = re.sub(r'[<>:"/\\|?*]+', "-", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .-")
    cleaned = cleaned[:max_length].strip(" .-")
    return cleaned or fallback


def html_escape(text: str) -> str:
    """Escape user-supplied text for ``parse_mode=HTML``."""
    return html.escape(text or "", quote=False)


def truncate(text: str, limit: int, suffix: str = "…") -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - len(suffix))].rstrip() + suffix


def make_temp_dir(root: Path, prefix: str = "job-") -> Path:
    """Create a unique working directory under ``root``."""
    root.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=str(root)))


def remove_dir(path: Path | str | None) -> bool:
    """Best-effort recursive delete. Returns True when something was removed."""
    if not path:
        return False
    target = Path(path)
    if not target.exists():
        return False
    try:
        shutil.rmtree(target, ignore_errors=True)
        return not target.exists()
    except OSError:
        return False


def disk_free_bytes(path: Path | str) -> int:
    try:
        return shutil.disk_usage(str(path)).free
    except OSError:
        return 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def elapsed(started: datetime) -> str:
    delta: timedelta = utc_now() - started
    return human_duration(delta.total_seconds())


def parse_quality_label(label: str) -> int | None:
    """``"1080p"`` → ``1080``; ``"best"``/``"audio"`` → ``None``."""
    match = re.fullmatch(r"(\d{3,4})p", (label or "").strip().lower())
    return int(match.group(1)) if match else None


def clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


# --------------------------------------------------------------------------- #
# Host budget
# --------------------------------------------------------------------------- #
def cpu_budget() -> float:
    """How many CPUs this container may actually use — not how many the host has.

    ``os.cpu_count()`` reports the *machine's* cores, which is exactly the wrong number
    inside a container: a Render free instance sees dozens of cores and is allowed 0.1 of
    one. The cgroup limits are the honest answer, and they decide how many parallel range
    workers make sense.
    """
    # cgroup v2: "max" or "<quota> <period>"
    try:
        quota, period = Path("/sys/fs/cgroup/cpu.max").read_text().split()
        if quota != "max":
            return max(0.1, int(quota) / int(period))
    except (OSError, ValueError):
        pass

    # cgroup v1: quota -1 means "unlimited"
    try:
        quota = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_quota_us").read_text().strip())
        period = int(Path("/sys/fs/cgroup/cpu/cpu.cfs_period_us").read_text().strip())
        if quota > 0 and period > 0:
            return max(0.1, quota / period)
    except (OSError, ValueError):
        pass

    return float(os.cpu_count() or 1)


def auto_workers(budget: float | None = None, *, low: int = 8, high: int = 32) -> int:
    """Parallel range workers for the download engine.

    Range downloads are latency-bound, so a handful of workers help even on a throttled
    container; a real core earns more. The engine clamps to 1-32 itself.
    """
    cpus = cpu_budget() if budget is None else budget
    return int(clamp(round(cpus * 16), low, high))


def budget_note(budget: float | None = None) -> str:
    """A one-line description of the host's CPU budget, for logs and ``/diag``."""
    cpus = cpu_budget() if budget is None else budget
    cores = f"{cpus:.2f}".rstrip("0").rstrip(".")
    if cpus < 1:
        return (
            f"{cores} CPU — a throttled container. Downloads and muxing are CPU-bound here, so expect "
            "slow throughput: a paid instance or a VPS is the fix, not a setting."
        )
    return f"{cores} CPU — {auto_workers(cpus)} download workers, {cpus:.0f}× parallel work."
