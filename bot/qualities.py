"""Quality options, built from what the link actually offers.

TypeTube reports the streams a video really has, so the picker shows those and nothing
else: a 240p clip offers 240p, not a decorative 1080p button that silently downloads
something smaller.

What each button maps to:

* ``720p``  → the best stream at or below 720p, preferring H.264/MP4 so Telegram can play it.
* ``best``  → the highest stream the video has.
* ``audio`` → the audio track, muxed to M4A by the bot when the source is not already M4A.

Video streams carry no size in TypeTube's metadata, so the picker does not invent one — it
shows the resolution and codec instead. Audio keeps an honest estimate, because bitrate and
duration are both known.
"""

from __future__ import annotations

from dataclasses import dataclass

from bot.typetube import Stream, Track
from config import Settings

#: Telegram renders a keyboard better with a handful of rows than with a wall of buttons.
MAX_VIDEO_OPTIONS = 7


@dataclass(frozen=True)
class QualityOption:
    """One button in the quality picker."""

    key: str  # "720p" | "best" | "audio"
    label: str  # what the user reads
    kind: str  # "video" | "audio"
    target: str  # value handed to TypeTube's -f (or "" for audio)
    height: int | None = None
    resolution: str = ""
    codec: str = ""
    bitrate: int = 0
    estimated_size: int = 0
    description: str = ""

    @property
    def is_audio(self) -> bool:
        return self.kind == "audio"

    @property
    def is_best(self) -> bool:
        return self.key == "best"


def _video_option(height: int | None, label: str, stream: Stream, *, key: str) -> QualityOption:
    return QualityOption(
        key=key,
        label=label,
        kind="video",
        target=f"{height}p" if height else "highest",
        height=height,
        resolution=stream.resolution or "",
        codec=stream.codec,
        description=f"{stream.resolution or label} · {stream.codec or 'video'}",
    )


def _audio_option(track: Track, settings: Settings, capability_bytes: int | None = None) -> QualityOption | None:
    stream = track.best_audio()
    if stream is None:
        return None
    bitrate = stream.bitrate or 0
    size = int(bitrate * track.duration / 8) if bitrate and track.duration else 0
    kbps = f"{round(bitrate / 1000)} kbps" if bitrate else "audio"
    label = f"🎵 Audio · M4A · {kbps}"
    description = f"{kbps} {stream.codec or 'audio'}"
    # Honest note when the track is longer than what the transport can actually carry.
    if capability_bytes and size and size > capability_bytes:
        description += " · longer than Telegram will accept from this bot"
    return QualityOption(
        key="audio",
        label=label,
        kind="audio",
        target="",
        bitrate=bitrate,
        estimated_size=size,
        codec=stream.codec or "AAC",
        description=description,
    )


def video_heights(track: Track) -> list[int]:
    """Distinct heights the video really offers, ascending.

    Streams Telegram plays reliably (H.264 in MP4) win; if a video has none of those, its
    other streams are used so the video is still offered.
    """
    streams = [stream for stream in track.video_streams if stream.height]
    if not streams:
        return []
    safe = [stream for stream in streams if stream.telegram_safe]
    pool = safe or streams
    return sorted({stream.height for stream in pool if stream.height})


def build_options(track: Track, settings: Settings, *, capability_bytes: int | None = None) -> list[QualityOption]:
    """Every option to offer for ``track``: best, the real heights, then audio."""
    options: list[QualityOption] = []
    heights = video_heights(track)

    if heights:
        if len(heights) > MAX_VIDEO_OPTIONS:
            heights = heights[-MAX_VIDEO_OPTIONS:]
        top = heights[-1]
        best_stream = track.stream_for(top)
        if best_stream is not None:
            options.append(_video_option(top, f"⭐️ Best · {top}p", best_stream, key="best"))

        for height in reversed(heights[:-1]):  # highest first reads better under "Best"
            stream = track.stream_for(height)
            if stream is None:
                continue
            label = f"{height}p" + (f" · {stream.resolution}" if stream.resolution else "")
            options.append(_video_option(height, label, stream, key=f"{height}p"))

    audio = _audio_option(track, settings, capability_bytes)
    if audio is not None:
        options.append(audio)

    return options


def find_option(options: list[QualityOption], key: str) -> QualityOption | None:
    for option in options:
        if option.key == key:
            return option
    return None


def default_option(options: list[QualityOption], settings: Settings) -> QualityOption | None:
    """The option to recommend: the configured default height, else the best available."""
    wanted = settings.default_quality.strip().lower()
    if wanted and wanted != "best":
        match = find_option(options, wanted)
        if match is not None:
            return match
    best = find_option(options, "best")
    if best is not None:
        return best
    videos = [option for option in options if not option.is_audio]
    return videos[0] if videos else (options[0] if options else None)


def smaller_video_options(options: list[QualityOption], than: QualityOption | None) -> list[QualityOption]:
    """Video options below ``than`` — used to suggest a retry after a failure."""
    ceiling = than.height if than is not None and than.height else None
    if ceiling is None:
        return []
    smaller = [option for option in options if option.kind == "video" and option.height and option.height < ceiling]
    return sorted(smaller, key=lambda option: option.height or 0, reverse=True)


__all__ = [
    "MAX_VIDEO_OPTIONS",
    "QualityOption",
    "build_options",
    "default_option",
    "find_option",
    "smaller_video_options",
    "video_heights",
]
