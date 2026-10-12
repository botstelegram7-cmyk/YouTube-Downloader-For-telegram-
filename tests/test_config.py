"""Configuration: defaults, overrides and the limit auto-detection."""

from __future__ import annotations

from pathlib import Path

import pytest

from config import (
    CLOUD_UPLOAD_LIMIT_MB,
    LOCAL_UPLOAD_LIMIT_MB,
    ConfigError,
    Settings,
    load_settings,
    require_settings,
)

VALID = {"BOT_TOKEN": "123456789:" + "A" * 35}


def build(**env) -> Settings:
    return load_settings({**VALID, **env})


def test_defaults_are_sane():
    settings = build()
    assert settings.port == 8080
    assert settings.max_concurrent_jobs == 2
    assert settings.audio_format == "m4a"
    assert settings.download_dir == Path("/tmp/ytbot-downloads")
    assert settings.progress_interval == 2.0
    assert 720 in settings.quality_ladder


def test_cloud_api_caps_uploads_at_50_mb():
    settings = build()
    assert settings.uses_local_api is False
    assert settings.upload_limit_mb == CLOUD_UPLOAD_LIMIT_MB
    assert settings.upload_limit_bytes == 50 * 1024 * 1024
    assert any("50 MB" in note for note in settings.warnings())


def test_local_api_server_raises_the_limit_to_2000_mb():
    settings = build(TELEGRAM_API_BASE="http://telegram-bot-api:8081")
    assert settings.uses_local_api is True
    assert settings.api_mode == "local Bot API server"
    assert settings.upload_limit_mb == LOCAL_UPLOAD_LIMIT_MB
    assert settings.api_base == "http://telegram-bot-api:8081"
    # No "you are limited to 50 MB" warning when the ceiling has been lifted.
    assert not any("cloud Bot API" in note for note in settings.warnings())


def test_the_cloud_url_with_a_trailing_slash_is_not_treated_as_self_hosted():
    settings = build(TELEGRAM_API_BASE="https://api.telegram.org/")
    assert settings.uses_local_api is False
    assert settings.upload_limit_mb == CLOUD_UPLOAD_LIMIT_MB


def test_max_upload_mb_overrides_the_detected_value():
    assert build(MAX_UPLOAD_MB="120").upload_limit_mb == 120
    assert build(MAX_UPLOAD_MB="0").upload_limit_mb == CLOUD_UPLOAD_LIMIT_MB
    assert build(TELEGRAM_API_BASE="http://x:8081", MAX_UPLOAD_MB="300").upload_limit_mb == 300


def test_asking_for_more_than_the_api_allows_is_a_problem():
    problems = build(MAX_UPLOAD_MB="900").validate()
    assert any("MAX_UPLOAD_MB=900 exceeds" in problem for problem in problems)


def test_webhook_detection_and_target():
    settings = build(WEBHOOK_URL="https://bot.example.com/", WEBHOOK_PATH="hooks/telegram")
    assert settings.runs_webhook is True
    assert settings.webhook_target == "https://bot.example.com/hooks/telegram"


def test_polling_is_used_when_no_public_url_is_configured():
    settings = build()
    assert settings.runs_webhook is False
    assert any("long polling" in note for note in settings.warnings())


def test_use_polling_wins_over_a_webhook_url():
    settings = build(WEBHOOK_URL="https://bot.example.com", USE_POLLING="true")
    assert settings.runs_webhook is False


def test_missing_token_is_reported_not_fatal_to_import():
    settings = load_settings({})
    assert "BOT_TOKEN is required" in " ".join(settings.validate())


def test_a_malformed_token_is_rejected():
    problems = load_settings({"BOT_TOKEN": "not-a-token"}).validate()
    assert any("BotFather token" in problem for problem in problems)


def test_boolean_and_number_parsing():
    settings = build(CLEANUP_AFTER_UPLOAD="no", MAX_CONCURRENT_JOBS="4", PROGRESS_INTERVAL="1.5")
    assert settings.cleanup_after_upload is False
    assert settings.max_concurrent_jobs == 4
    assert settings.progress_interval == 1.5


def test_invalid_values_raise_a_readable_error():
    with pytest.raises(ConfigError) as error:
        load_settings({**VALID, "PORT": "not-a-number"})
    assert "PORT must be an integer" in str(error.value)


def test_require_settings_returns_a_valid_object():
    settings = require_settings(VALID)
    assert settings.bot_token.startswith("123456789:")


def test_summary_never_leaks_the_token():
    summary = build().summary()
    assert "bot_token" not in summary
    assert "A" * 35 not in str(summary)
    assert summary["upload_limit_mb"] == CLOUD_UPLOAD_LIMIT_MB


def test_quality_ladder_can_be_restricted():
    settings = build(QUALITY_LADDER="360,720")
    assert settings.quality_ladder == (360, 720)


def test_missing_cookies_file_is_reported():
    problems = build(COOKIES_FILE="/nope/cookies.txt").validate()
    assert any("COOKIES_FILE does not exist" in problem for problem in problems)
