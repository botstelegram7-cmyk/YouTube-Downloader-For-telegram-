"""Configuration: defaults, limits and the ways a deploy can be misconfigured.

The upload ceiling gets the most attention, because getting it wrong is invisible until a
user asks for a big file — and because an over-strict check once took a working deployment
down instead of warning.
"""

from __future__ import annotations

import pytest

from config import (
    CLOUD_UPLOAD_LIMIT_MB,
    LOCAL_UPLOAD_LIMIT_MB,
    MT_PREMIUM_UPLOAD_LIMIT_MB,
    MT_UPLOAD_LIMIT_MB,
    ConfigError,
    Settings,
    load_settings,
    require_settings,
)

TOKEN = "123456789:" + "A" * 35
SESSION = {
    "TELEGRAM_API_ID": "1234567",
    "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
    "TELEGRAM_SESSION_STRING": "BQ" + "x" * 40,
}


def build(**env) -> Settings:
    return load_settings({"BOT_TOKEN": TOKEN, **env})


def joined(items) -> str:
    return " | ".join(items)


# -- defaults ---------------------------------------------------------------- #
def test_only_a_token_is_required():
    settings = build()
    assert settings.validate() == []
    assert settings.upload_limit_mb == CLOUD_UPLOAD_LIMIT_MB
    assert settings.api_mode == "cloud Bot API"
    assert settings.transport == "bot"
    assert settings.runs_webhook is False
    assert settings.default_quality == "720p"
    assert settings.notify_chat_id == 0


def test_the_bot_id_comes_from_the_token():
    assert build().bot_id == 123456789
    assert load_settings({"BOT_TOKEN": "not-a-token"}).bot_id == 0


# -- the deploy regression: a too-large ceiling warns, it never aborts --------- #
def test_a_ceiling_above_the_transport_is_clamped_not_fatal():
    """MAX_UPLOAD_MB=500 on the cloud API must boot, clamp, and explain itself."""
    settings = build(MAX_UPLOAD_MB="500")

    assert settings.validate() == [], "an unattainable ceiling must not stop the bot booting"
    assert settings.upload_limit_mb == CLOUD_UPLOAD_LIMIT_MB
    assert settings.max_upload_mb == 500, "the requested value is remembered for /status"
    assert "clamped" in joined(settings.warnings())


def test_a_huge_ceiling_is_clamped_to_what_is_possible():
    assert build(MAX_UPLOAD_MB="999999").upload_limit_mb == CLOUD_UPLOAD_LIMIT_MB


def test_a_smaller_ceiling_is_honoured_exactly():
    assert build(MAX_UPLOAD_MB="20").upload_limit_mb == 20


# -- MTProto: the 2 GB route ------------------------------------------------- #
def test_a_session_raises_the_ceiling_and_the_transport():
    settings = build(**SESSION, MAX_UPLOAD_MB="500")

    assert settings.mtproto_ready is True
    assert settings.transport == "mtproto"
    assert settings.mtproto_cap_mb == MT_UPLOAD_LIMIT_MB
    assert settings.upload_cap_mb == MT_UPLOAD_LIMIT_MB
    assert settings.upload_limit_mb == 500, "the requested ceiling is reachable now"
    assert settings.validate() == []


def test_without_an_explicit_ceiling_a_session_allows_two_gigabytes():
    assert build(**SESSION).upload_limit_mb == MT_UPLOAD_LIMIT_MB


def test_premium_raises_the_session_ceiling_to_four_gigabytes():
    settings = build(**SESSION, MT_PREMIUM_ACCOUNT="1")
    assert settings.mtproto_cap_mb == MT_PREMIUM_UPLOAD_LIMIT_MB
    assert settings.upload_limit_mb == MT_PREMIUM_UPLOAD_LIMIT_MB


def test_the_alternative_spellings_of_the_credentials_work():
    settings = load_settings(
        {
            "BOT_TOKEN": TOKEN,
            "API_ID": SESSION["TELEGRAM_API_ID"],
            "API_HASH": SESSION["TELEGRAM_API_HASH"],
            "SESSION_STRING": SESSION["TELEGRAM_SESSION_STRING"],
        }
    )
    assert settings.mtproto_ready is True


def test_a_half_configured_session_is_reported_with_the_missing_name():
    problems = build(TELEGRAM_API_ID=SESSION["TELEGRAM_API_ID"]).validate()
    assert any("TELEGRAM_SESSION_STRING" in problem for problem in problems)
    assert any("TELEGRAM_API_HASH" in problem for problem in problems)


def test_forcing_mtproto_without_credentials_falls_back_and_warns():
    settings = build(UPLOAD_TRANSPORT="mtproto")
    assert settings.transport == "bot"
    assert "incomplete" in joined(settings.warnings())


def test_an_unknown_transport_is_refused():
    problems = build(UPLOAD_TRANSPORT="carrier-pigeon").validate()
    assert any("UPLOAD_TRANSPORT" in problem for problem in problems)


def test_connection_count_is_bounded():
    assert build(MT_CONNECTIONS="8").mt_connections == 8
    with pytest.raises(ConfigError):
        build(MT_CONNECTIONS="99")


# -- local Bot API server: the alternative route ------------------------------ #
def test_a_local_bot_api_server_raises_the_bot_api_ceiling():
    settings = build(TELEGRAM_API_BASE="http://telegram-bot-api:8081")
    assert settings.uses_local_api is True
    assert settings.api_upload_cap_mb == LOCAL_UPLOAD_LIMIT_MB
    assert settings.upload_limit_mb == LOCAL_UPLOAD_LIMIT_MB
    assert settings.api_mode == "local Bot API server"


# -- validation and secrets --------------------------------------------------- #
def test_a_missing_or_malformed_token_is_a_problem():
    assert "BOT_TOKEN is required" in joined(load_settings({}).validate())
    assert "does not look like" in joined(load_settings({"BOT_TOKEN": "not-a-token"}).validate())


def test_require_settings_raises_for_an_unusable_environment():
    with pytest.raises(ConfigError):
        require_settings({})
    assert require_settings({"BOT_TOKEN": TOKEN}).bot_token == TOKEN


def test_webhook_detection_and_target():
    settings = build(WEBHOOK_URL="https://bot.example.com/")
    assert settings.runs_webhook is True
    assert settings.webhook_target == "https://bot.example.com/telegram"

    render = build(RENDER_EXTERNAL_URL="https://render.example.com")
    assert render.runs_webhook is True


def test_polling_can_be_forced():
    assert build(WEBHOOK_URL="https://bot.example.com", USE_POLLING="true").runs_webhook is False


def test_a_bad_log_level_is_a_problem():
    assert "LOG_LEVEL" in joined(build(LOG_LEVEL="LOUD").validate())


def test_the_summary_shows_the_transport_and_never_a_secret():
    settings = build(**SESSION, MAX_UPLOAD_MB="1500")
    summary = settings.summary()

    assert summary["upload_transport"] == "mtproto"
    assert summary["mtproto"] == "ready"
    assert summary["upload_limit_mb"] == 1500
    assert summary["upload_cap_mb"] == MT_UPLOAD_LIMIT_MB

    rendered = repr(summary)
    assert SESSION["TELEGRAM_API_HASH"] not in rendered
    assert SESSION["TELEGRAM_SESSION_STRING"] not in rendered
    assert TOKEN not in rendered


def test_warnings_never_pretend_the_limit_is_higher_than_it_is():
    notes = joined(build().warnings())
    assert "50 MB" in notes
    assert "TELEGRAM_SESSION_STRING" in notes
