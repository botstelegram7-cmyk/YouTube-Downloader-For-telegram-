"""Quality options derived from what a link actually offers.

Nothing here downloads anything: it reads the format list yt-dlp already returned
and turns it into the buttons a user sees, each with a realistic size estimate.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.utils import human_size
from config import Settings

#: Codecs/containers that play natively in Telegram and need no re-encoding.
PREFERRED_VIDEO_EXTS = ("mp4", "m4v")
PREFERRED_AUDIO_EXTS = ("m4a", "mp4", "aac")


@dataclass(frozen=True)
class QualityOption:
    """One button in the quality keyboard."""

    key: str
    label: str
    kind: str  # "video" | "audio"
    height: int | None
    filesize: int | None
    format_selector: str
    estimated: bool = False

    @property
    def callback_data(self) -> str:
        return f"dl:{self.key}"

    @property
    def is_audio(self) -> bool:
        return self.kind == "audio"


def _size_of(fmt: dict) -> int | None:
    for key in ("filesize", "filesize_approx"):
        value = fmt.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    tbr = fmt.get("tbr") or fmt.get("abr")
    duration = fmt.get("duration")
    if tbr and duration:
        try:
            return int(float(tbr) * 1000 / 8 * float(duration))
        except (TypeError, ValueError):
            return None
    return None


def _approx_size(fmt: dict, duration: float | None) -> tuple[int | None, bool]:
    """Size from yt-dlp metadata, falling back to a bitrate estimate."""
    direct = _size_of(fmt)
    if direct:
        return direct, False
    tbr = fmt.get("tbr") or fmt.get("abr")
    if tbr and duration:
        try:
            return int(float(tbr) * 1000 / 8 * float(duration)), True
        except (TypeError, ValueError):
            return None, True
    return None, True


def _video_formats(info: dict) -> list[dict]:
    return [f for f in info.get("formats") or [] if f.get("vcodec") not in (None, "none")]


def _audio_only_formats(info: dict) -> list[dict]:
    return [
        f
        for f in info.get("formats") or []
        if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
    ]


def available_heights(info: dict) -> list[int]:
    """Distinct video heights the link really offers, ascending."""
    heights = {int(f["height"]) for f in _video_formats(info) if isinstance(f.get("height"), (int, float))}
    return sorted(heights)


def _pick_video(info: dict, max_height: int | None) -> dict | None:
    """The format yt-dlp would prefer at or below ``max_height`` (mp4 first)."""
    candidates = [f for f in _video_formats(info) if max_height is None or (f.get("height") or 0) <= max_height]
    if not candidates:
        return None
    candidates.sort(
        key=lambda f: (
            f.get("ext") in PREFERRED_VIDEO_EXTS,
            f.get("height") or 0,
            f.get("tbr") or 0,
        ),
        reverse=True,
    )
    return candidates[0]


def _pick_audio(info: dict) -> dict | None:
    candidates = _audio_only_formats(info) or [
        f for f in info.get("formats") or [] if f.get("acodec") not in (None, "none")
    ]
    if not candidates:
        return None
    candidates.sort(
        key=lambda f: (f.get("ext") in PREFERRED_AUDIO_EXTS, f.get("abr") or f.get("tbr") or 0), reverse=True
    )
    return candidates[0]


def video_selector(height: int | None, *, settings: Settings) -> str:
    """yt-dlp format selector for a target height (``None`` = best available).

    Every chain ends in a bare ``best`` fallback: some sources (direct links,
    unusual extractors) do not report heights at all, and a picker choice must
    never fail the download just because the metadata was thin.
    """
    if height is None:
        return "bestvideo+bestaudio/best"
    strict = f"bestvideo[height<={height}]+bestaudio/best[height<={height}]/best"
    mp4_first = f"bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]/{strict}"
    return mp4_first if settings.audio_format == "m4a" else strict


def audio_selector(settings: Settings) -> str:
    if settings.audio_format == "m4a":
        return "bestaudio[ext=m4a]/bestaudio/best"
    return "bestaudio/best"


def estimate_size(
    info: dict, *, height: int | None, settings: Settings, audio_only: bool = False
) -> tuple[int | None, bool]:
    """Return ``(bytes, is_estimate)`` for a prospective download."""
    duration = info.get("duration")

    if audio_only:
        fmt = _pick_audio(info)
        if not fmt:
            return None, True
        if fmt.get("vcodec") not in (None, "none"):  # progressive fallback
            return _approx_size(fmt, duration)
        size, estimated = _approx_size(fmt, duration)
        if settings.audio_format == "m4a" and estimated and size:
            size = int(size * 1.05)
        return size, estimated

    video = _pick_video(info, height)
    if not video:
        return None, True

    total, estimated = _approx_size(video, duration)
    if video.get("acodec") not in (None, "none"):
        return total, estimated  # progressive: audio is already inside

    audio = _pick_audio(info)
    if audio:
        audio_size, audio_estimated = _approx_size(audio, duration)
        if audio_size:
            total = (total or 0) + audio_size
            estimated = estimated or audio_estimated
    return total, estimated


def _codecs_unknown(info: dict) -> bool:
    """True for direct links where yt-dlp reports one file with no codec details."""
    formats = info.get("formats") or []
    if not formats:
        return False
    return all(f.get("vcodec") in (None, "none") and f.get("acodec") in (None, "none") for f in formats)


def original_option(info: dict, settings: Settings) -> QualityOption | None:
    """The single "Original" button for a direct media URL (a raw .mp4, for example)."""
    formats = info.get("formats") or []
    if not formats:
        return None
    size = _size_of(formats[0])
    label = "Original" + (f" · {human_size(size)}" if size else " · as provided")
    return QualityOption(
        key="best",
        label=label,
        kind="video",
        height=None,
        filesize=size,
        format_selector="best",
        estimated=size is None,
    )


def build_options(info: dict, settings: Settings) -> list[QualityOption]:
    """Buttons for every quality this link can actually deliver."""
    heights = available_heights(info)
    options: list[QualityOption] = []

    max_height = max(heights) if heights else None

    if max_height:
        offered = [h for h in settings.quality_ladder if any(h == height for height in heights)]
        # A ladder entry may fall between real heights (e.g. 720p offered, 1080p the
        # only stream above it): keep the ones that are genuinely reachable.
        for ladder_height in settings.quality_ladder:
            if ladder_height > max_height:
                continue
            if ladder_height in offered:
                options.append(_video_option(info, ladder_height, settings, label=str(ladder_height) + "p"))
            # else: the source has nothing at that height; skip it rather than lie.

        if f"{max_height}p" not in {option.key for option in options}:
            options.append(_video_option(info, max_height, settings, label=f"{max_height}p"))

        best_size, best_estimated = estimate_size(info, height=None, settings=settings)
        options.append(
            QualityOption(
                key="best",
                label=f"Best · {max_height}p · {human_size(best_size)}".strip(),
                kind="video",
                height=max_height,
                filesize=best_size,
                format_selector=video_selector(None, settings=settings),
                estimated=best_estimated,
            )
        )

    if not options and _codecs_unknown(info):
        # A direct link: yt-dlp knows the URL but not what is inside it.
        original = original_option(info, settings)
        if original is not None:
            options.append(original)

    if settings.offer_audio_only and _pick_audio(info):
        audio_size, audio_estimated = estimate_size(info, height=None, settings=settings, audio_only=True)
        options.append(
            QualityOption(
                key="audio",
                label=f"Audio · {settings.audio_format.upper()} · {human_size(audio_size)}",
                kind="audio",
                height=None,
                filesize=audio_size,
                format_selector=audio_selector(settings),
                estimated=audio_estimated,
            )
        )

    return options


def _video_option(info: dict, height: int, settings: Settings, *, label: str) -> QualityOption:
    size, estimated = estimate_size(info, height=height, settings=settings)
    if size:
        # "≈" tells the user this is a bitrate-based guess, not the real size.
        size_text = f"≈{human_size(size)}" if estimated else human_size(size)
        label = f"{label} · {size_text}"
    return QualityOption(
        key=f"{height}p",
        label=label,
        kind="video",
        height=height,
        filesize=size,
        format_selector=video_selector(height, settings=settings),
        estimated=estimated,
    )


def find_option(options: list[QualityOption], key: str) -> QualityOption | None:
    for option in options:
        if option.key == key:
            return option
    return None


def sort_options(options: list[QualityOption]) -> list[QualityOption]:
    """Ladder order for display: smallest first, then Best, then audio."""

    def sort_key(option: QualityOption) -> tuple[int, int]:
        if option.key == "best":
            return (1, 0)
        if option.is_audio:
            return (2, 0)
        return (0, option.height or 0)

    return sorted(options, key=sort_key)
