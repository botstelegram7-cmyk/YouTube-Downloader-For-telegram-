"""Quality options must describe what a video really offers — nothing more.

Streams come from TypeTube's metadata, so the picker shows the real heights, prefers the
codecs Telegram can actually play, and never invents a size for a video stream (TypeTube
does not report one).
"""

from __future__ import annotations

from bot.qualities import (
    MAX_VIDEO_OPTIONS,
    build_options,
    default_option,
    find_option,
    smaller_video_options,
    video_heights,
)
from bot.typetube import Stream, Track
from config import load_settings

SETTINGS = load_settings({"BOT_TOKEN": "123456789:" + "A" * 35})

M4A = 'audio/mp4; codecs="mp4a.40.2"'
AVC = 'video/mp4; codecs="avc1.4d401f"'
VP9 = 'video/webm; codecs="vp9"'


def stream(itag: int, height: int | None, mime: str = AVC, bitrate: int = 0) -> Stream:
    return Stream(
        itag=itag,
        quality=f"{height}p" if height else "",
        mime=mime,
        url="https://example.invalid/stream",
        resolution=f"1920x{height}" if height else "",
        bitrate=bitrate,
    )


def track(*, heights=(360, 720, 1080), audio_bitrate=128_000, duration=100.0, safe=True) -> Track:
    mime = AVC if safe else VP9
    videos = tuple(stream(100 + index, height, mime) for index, height in enumerate(heights))
    audio = (Stream(itag=140, quality="130kbps AAC", mime=M4A, url="u", resolution="", bitrate=audio_bitrate),)
    return Track(
        id="x",
        title="Test video",
        author="Test channel",
        duration=duration,
        thumbnail="",
        video_streams=videos,
        audio_streams=audio if audio_bitrate else (),
        latency_ms=100,
        raw={},
        url="https://www.youtube.com/watch?v=abcdefghijk",
    )


def keys(options) -> set[str]:
    return {option.key for option in options}


def test_only_the_heights_the_video_has_are_offered():
    options = build_options(track(), SETTINGS)
    assert keys(options) == {"best", "720p", "360p", "audio"}
    for missing in ("144p", "240p", "480p", "1440p", "2160p"):
        assert missing not in keys(options)


def test_best_is_the_highest_real_height_and_targets_it():
    options = build_options(track(), SETTINGS)
    best = find_option(options, "best")
    assert best.height == 1080
    assert best.target == "1080p"
    assert "1080" in best.label


def test_heights_are_deduplicated_and_sorted():
    assert video_heights(track(heights=(240, 240, 1080, 720))) == [240, 720, 1080]


def test_streams_telegram_cannot_play_are_not_offered_when_h264_exists():
    """A VP9-only height would produce a file Telegram often will not play."""
    mixed = Track(
        id="x",
        title="t",
        author="",
        duration=10,
        thumbnail="",
        video_streams=(stream(18, 360, AVC), stream(248, 1080, VP9)),
        audio_streams=(),
        latency_ms=0,
        raw={},
        url="u",
    )
    assert video_heights(mixed) == [360]


def test_vp9_is_used_when_it_is_all_the_video_has():
    only_vp9 = track(heights=(720,), safe=False)
    assert video_heights(only_vp9) == [720]
    assert keys(build_options(only_vp9, SETTINGS)) == {"best", "audio"}


def test_the_picker_is_capped_but_keeps_the_highest_qualities():
    options = build_options(track(heights=tuple(range(144, 1440, 96))), SETTINGS)
    videos = [option for option in options if not option.is_audio]
    heights = [option.height for option in videos]

    assert len(videos) <= MAX_VIDEO_OPTIONS
    assert heights[0] == 1392, "the highest quality is always offered"
    assert 144 not in heights, "the ladder is trimmed from the bottom, not the top"
    assert heights == sorted(heights, reverse=True), "highest quality first"
    assert len(set(heights)) == len(heights), "no duplicate buttons"


def test_audio_is_offered_with_an_estimate_from_bitrate_and_duration():
    audio = find_option(build_options(track(audio_bitrate=128_000, duration=100.0), SETTINGS), "audio")
    assert audio is not None
    assert audio.estimated_size == int(128_000 * 100 / 8)
    assert "kbps" in audio.label


def test_an_audio_track_longer_than_the_transport_can_carry_says_so():
    options = build_options(track(audio_bitrate=256_000, duration=7200.0), SETTINGS, capability_bytes=50 * 1024 * 1024)
    audio = find_option(options, "audio")
    assert "Telegram will accept" in audio.description


def test_default_option_prefers_the_configured_quality():
    options = build_options(track(), SETTINGS)  # DEFAULT_QUALITY=720p
    assert default_option(options, SETTINGS).key == "720p"

    only_144 = build_options(track(heights=(144,)), SETTINGS)
    assert default_option(only_144, SETTINGS).key == "best"


def test_audio_only_videos_offer_just_audio():
    options = build_options(track(heights=()), SETTINGS)
    assert keys(options) == {"audio"}


def test_a_video_with_no_streams_offers_nothing():
    empty = Track(
        id="x",
        title="t",
        author="",
        duration=0,
        thumbnail="",
        video_streams=(),
        audio_streams=(),
        latency_ms=0,
        raw={},
        url="u",
    )
    assert build_options(empty, SETTINGS) == []


def test_smaller_options_are_the_retry_ladder():
    options = build_options(track(), SETTINGS)
    chosen = find_option(options, "best")
    smaller = smaller_video_options(options, chosen)
    assert [option.key for option in smaller] == ["720p", "360p"]

    smallest = find_option(options, "360p")
    assert smaller_video_options(options, smallest) == []


def test_find_option_returns_none_for_an_unknown_key():
    options = build_options(track(), SETTINGS)
    assert find_option(options, "480p") is None
    assert find_option(options, "720p").height == 720
