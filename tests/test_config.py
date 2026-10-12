"""Configuration: defaults, and the fact that the application sets no size limits.

The interesting assertions here are negative ones — that no setting can lower Telegram's
ceilings, that no user-session credential exists, and that the capability reported to users
is the one the chosen transport really has.
"""

from __future__ import annotations

import pytest

from config import (
    BOT_API_UPLOAD_MB,
    LOCAL_BOT_API_UPLOAD_MB,
    MB,
    MT_UPLOAD_MB,
    ConfigError,
    Settings,
    load_settings,
    require_settings,
)

TOKEN = "123456789:" + "A" * 35
API_KEY = {"TELEGRAM_API_ID": "1234567", "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef"}


def build(**env) -> Settings:
    return load_settings({"BOT_TOKEN": TOKEN, **env})


def joined(items) -> str:
    return " | ".join(items)


def test_only_a_token_is_required():
    settings = build()
    assert settings.validate() == []
    assert settings.transport == "bot"
    assert settings.runs_webhook is False
    assert settings.notify_chat_id == 0
    assert settings.max_duration_minutes == 0


# -- no artificial limits ----------------------------------------------------- #
def test_there_is_no_variable_that_lowers_the_ceiling():
    """MAX_UPLOAD_MB is gone: an operator cannot cap file size, and nothing silently clips."""
    settings = build()
    for removed in ("max_upload_mb", "upload_limit_mb", "upload_limit_bytes"):
        assert not hasattr(settings, removed), f"{removed} should not exist"


def test_the_capability_is_telegram_s_own_number():
    assert build().upload_capability_mb == BOT_API_UPLOAD_MB
    assert build(**API_KEY).upload_capability_mb == MT_UPLOAD_MB
    assert build(TELEGRAM_API_BASE="http://api.local:8081").upload_capability_mb == LOCAL_BOT_API_UPLOAD_MB
    assert build().upload_capability_bytes == BOT_API_UPLOAD_MB * MB


def test_a_local_bot_api_server_is_the_bot_api_ceiling_not_a_policy():
    settings = build(TELEGRAM_API_BASE="http://telegram-bot-api:8081")
    assert settings.uses_local_api is True
    assert settings.bot_api_capability_mb == LOCAL_BOT_API_UPLOAD_MB
    assert settings.api_mode == "local Bot API server"


# -- MTProto with the bot token, and nothing else ----------------------------- #
def test_api_id_and_hash_enable_mtproto_without_any_login():
    settings = build(**API_KEY)
    assert settings.mtproto_ready is True
    assert settings.transport == "mtproto"
    assert settings.mtproto_capability_mb == MT_UPLOAD_MB
    assert settings.validate() == []


def test_no_user_session_credential_exists():
    """The old design asked for a logged-in account; this one must never need it."""
    settings = build(**API_KEY)
    for removed in ("session_string", "storage_chat_id", "mt_premium", "max_upload_mb"):
        assert not hasattr(settings, removed), f"{removed} should not exist"


def test_the_alternative_spellings_of_the_api_key_work():
    settings = load_settings({"BOT_TOKEN": TOKEN, "API_ID": "1234567", "API_HASH": "abcdef"})
    assert settings.mtproto_ready is True


def test_a_half_configured_key_pair_is_named_in_the_error():
    problems = build(TELEGRAM_API_ID="1234567").validate()
    assert any("TELEGRAM_API_HASH" in problem for problem in problems)
    assert any("no user account" in problem for problem in problems)


def test_forcing_mtproto_without_credentials_falls_back_to_the_bot_api():
    settings = build(UPLOAD_TRANSPORT="mtproto")
    assert settings.transport == "bot"
    assert settings.upload_capability_mb == BOT_API_UPLOAD_MB
    assert "UPLOAD_TRANSPORT=mtproto was requested" in joined(settings.warnings())


def test_an_unknown_transport_is_refused():
    assert any("UPLOAD_TRANSPORT" in problem for problem in build(UPLOAD_TRANSPORT="pigeon").validate())


def test_connection_count_is_bounded_by_telegram():
    assert build(MT_CONNECTIONS="8").mt_connections == 8
    with pytest.raises(ConfigError):
        build(MT_CONNECTIONS="99")


# -- the host's constraint is separate ---------------------------------------- #
def test_disk_headroom_is_a_hosting_knob_not_a_file_cap():
    settings = build(MIN_FREE_DISK_MB="1024")
    assert settings.min_free_disk_mb == 1024
    assert settings.min_free_disk_bytes == 1024 * MB
    assert "very low" in joined(build(MIN_FREE_DISK_MB="32").warnings())


def test_duration_limit_defaults_to_unlimited():
    assert build().max_duration_minutes == 0
    assert build(MAX_DURATION_MINUTES="90").max_duration_minutes == 90


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
    assert build(RENDER_EXTERNAL_URL="https://render.example.com").runs_webhook is True


def test_polling_can_be_forced():
    assert build(WEBHOOK_URL="https://bot.example.com", USE_POLLING="true").runs_webhook is False


def test_a_bad_log_level_is_a_problem():
    assert "LOG_LEVEL" in joined(build(LOG_LEVEL="LOUD").validate())


def test_the_summary_reports_capabilities_and_never_a_secret():
    settings = build(**API_KEY)
    summary = settings.summary()

    assert summary["upload_transport"] == "mtproto"
    assert summary["upload_capability_mb"] == MT_UPLOAD_MB
    assert summary["bot_api_capability_mb"] == BOT_API_UPLOAD_MB
    assert summary["mtproto"] == "ready (bot token)"

    rendered = repr(summary)
    assert TOKEN not in rendered
    assert API_KEY["TELEGRAM_API_HASH"] not in rendered


def test_warnings_explain_how_to_raise_the_ceiling_without_a_login():
    notes = joined(build().warnings())
    assert f"{BOT_API_UPLOAD_MB} MB" in notes
    assert "TELEGRAM_API_ID" in notes
    assert "session string" not in notes.lower().replace("no session string", "")
