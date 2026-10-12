"""Quality options must describe what a link really offers — nothing more."""

from __future__ import annotations

import pytest

from bot.qualities import (
    audio_selector,
    available_heights,
    build_options,
    estimate_size,
    find_option,
    sort_options,
    video_selector,
)
from config import Settings, load_settings

SETTINGS = load_settings({"BOT_TOKEN": "123456789:" + "A" * 35})

#: A link with 360p progressive, 720p/1080p video-only, M4A and Opus audio.
INFO = {
    "title": "Test video",
    "duration": 100,
    "uploader": "Test channel",
    "formats": [
        {
            "format_id": "18",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "aac",
            "height": 360,
            "tbr": 500,
            "filesize": 6_000_000,
        },
        {
            "format_id": "136",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "none",
            "height": 720,
            "tbr": 1500,
            "filesize": 18_000_000,
        },
        {
            "format_id": "137",
            "ext": "mp4",
            "vcodec": "avc1",
            "acodec": "none",
            "height": 1080,
            "tbr": 2400,
            "filesize": 30_000_000,
        },
        {"format_id": "140", "ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 1_600_000},
        {"format_id": "251", "ext": "webm", "vcodec": "none", "acodec": "opus", "abr": 160, "filesize": 2_000_000},
    ],
}


def test_only_heights_the_link_has_are_offered():
    options = build_options(INFO, SETTINGS)
    keys = {option.key for option in options}
    assert keys == {"360p", "720p", "1080p", "best", "audio"}
    # 144p, 240p, 480p, 1440p and 2160p are on the ladder but not in the source.
    for missing in ("144p", "240p", "480p", "1440p", "2160p"):
        assert missing not in keys


def test_available_heights_are_deduplicated_and_sorted():
    assert available_heights(INFO) == [360, 720, 1080]


def test_labels_carry_a_size_estimate():
    options = {option.key: option for option in build_options(INFO, SETTINGS)}
    assert "MB" in options["720p"].label
    assert options["720p"].label.startswith("720p")
    assert "1080p" in options["best"].label
    assert options["audio"].label.startswith("Audio · M4A")


def test_video_sizes_add_the_audio_track():
    size_720, _ = estimate_size(INFO, height=720, settings=SETTINGS)
    size_1080, _ = estimate_size(INFO, height=1080, settings=SETTINGS)
    assert size_720 == 18_000_000 + 1_600_000
    assert size_1080 == 30_000_000 + 1_600_000


def test_progressive_formats_are_not_double_counted():
    info = {
        "duration": 100,
        "formats": [{"ext": "mp4", "vcodec": "avc1", "acodec": "aac", "height": 360, "filesize": 5_000_000}],
    }
    size, _ = estimate_size(info, height=360, settings=SETTINGS)
    assert size == 5_000_000


def test_size_falls_back_to_bitrate_times_duration():
    info = {
        "duration": 200,
        "formats": [{"ext": "mp4", "vcodec": "avc1", "acodec": "none", "height": 480, "tbr": 2000}],
    }
    size, estimated = estimate_size(info, height=480, settings=SETTINGS)
    assert estimated is True
    assert size == pytest.approx(2000 * 1000 / 8 * 200, rel=0.01)


def test_audio_only_links_offer_audio_but_no_video():
    info = {
        "title": "Song",
        "duration": 200,
        "formats": [{"ext": "m4a", "vcodec": "none", "acodec": "mp4a", "abr": 128, "filesize": 3_200_000}],
    }
    options = build_options(info, SETTINGS)
    keys = {option.key for option in options}
    assert keys == {"audio"}
    assert "MB" in find_option(options, "audio").label


def test_progressive_only_links_offer_no_best_duplicate_of_a_missing_height():
    info = {
        "duration": 30,
        "formats": [{"ext": "mp4", "vcodec": "avc1", "acodec": "aac", "height": 240, "filesize": 900_000}],
    }
    keys = {option.key for option in build_options(info, SETTINGS)}
    # 240p is offered because the source has it; nothing above it is.
    assert "240p" in keys
    assert not any(key.endswith("p") and key not in ("240p",) for key in keys)


def test_format_selectors_request_the_right_ceiling():
    assert "height<=720" in video_selector(720, settings=SETTINGS)
    assert "height<=720" not in video_selector(None, settings=SETTINGS)
    assert video_selector(None, settings=SETTINGS).startswith("bestvideo+bestaudio")
    assert audio_selector(SETTINGS).startswith("bestaudio")
    mp3_settings = Settings(**{**SETTINGS.__dict__, "audio_format": "mp3"})
    assert audio_selector(mp3_settings) == "bestaudio[ext=m4a]/bestaudio/best" or "bestaudio" in audio_selector(
        mp3_settings
    )


def test_option_sort_order_puts_audio_last_and_best_before_it():
    ordered = sort_options(build_options(INFO, SETTINGS))
    keys = [option.key for option in ordered]
    assert keys[-1] == "audio"
    assert keys[-2] == "best"
    assert keys[:3] == ["360p", "720p", "1080p"]


def test_find_option_returns_none_for_unknown_keys():
    options = build_options(INFO, SETTINGS)
    assert find_option(options, "480p") is None
    assert find_option(options, "720p").height == 720


def test_audio_only_can_be_disabled():
    settings = Settings(**{**SETTINGS.__dict__, "offer_audio_only": False})
    keys = {option.key for option in build_options(INFO, settings)}
    assert "audio" not in keys


def test_callback_data_is_short_and_stable():
    option = find_option(build_options(INFO, SETTINGS), "720p")
    assert option.callback_data == "dl:720p"


def test_empty_format_list_produces_no_options():
    assert build_options({"formats": []}, SETTINGS) == []


def test_direct_links_without_geometry_still_offer_the_original_file():
    """A raw .mp4 URL arrives with vcodec/acodec unknown — offer it as-is."""
    info = {
        "title": "clip",
        "duration": 3,
        "direct": True,
        "formats": [{"format_id": "mp4", "ext": "mp4", "url": "http://x/clip.mp4", "vcodec": None, "filesize": 66_000}],
    }
    options = build_options(info, SETTINGS)
    assert [option.key for option in options] == ["best"]
    assert options[0].label.startswith("Original")
    assert options[0].format_selector == "best"


def test_formats_with_unknown_codecs_but_no_direct_flag_are_still_offered():
    assert build_options({"formats": [{"vcodec": None, "acodec": None}]}, SETTINGS)[0].key == "best"


def test_height_selector_always_has_a_last_resort_fallback():
    """Direct links report no heights; a pick must never fail the download."""
    for height in (144, 240, 360, 720, 1080):
        assert video_selector(height, settings=SETTINGS).endswith("/best")
