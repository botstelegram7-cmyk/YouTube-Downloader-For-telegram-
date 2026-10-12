"""Access rules, rich messages and the webhook secret — the operator surface.

The bot is open to everyone; what is restricted is the operator's view. These tests pin that
down, plus the two things that made a real deployment look offline: a webhook secret Telegram
refuses to accept, and a status message that could not be animated.
"""

from __future__ import annotations

import asyncio
import json
from typing import ClassVar

import pytest
from telegram.error import TelegramError

from bot.access import Access
from bot.assets import DOWNLOAD_ANIMATION, START_IMAGE, AssetLibrary
from bot.progress import ProgressCard
from bot.texts import diagnostics, owner_only, welcome
from config import load_settings, sanitize_webhook_secret

TOKEN = "123456789:" + "A" * 35


def settings(**env):
    return load_settings({"BOT_TOKEN": TOKEN, **env})


# -- the webhook secret ------------------------------------------------------- #
def test_a_base64_secret_is_cleaned_instead_of_being_refused_by_telegram():
    """Render's generateValue produced '…+…/…=' and setWebhook failed: the bot looked offline."""
    raw = "Ab+C/D/Ef=="
    fixed = sanitize_webhook_secret(raw)

    assert fixed == "AbCDEf"
    assert all(char.isalnum() or char in "_-" for char in fixed)


def test_the_bot_says_it_had_to_clean_the_secret():
    notes = " | ".join(settings(WEBHOOK_URL="https://x.onrender.com", WEBHOOK_SECRET="Ab+C/D/Ef==").warnings())
    assert "WEBHOOK_SECRET contained characters Telegram does not accept" in notes


def test_a_compliant_secret_is_left_alone():
    config = settings(WEBHOOK_URL="https://x.onrender.com", WEBHOOK_SECRET="a-b_C9")
    assert config.webhook_secret == "a-b_C9"
    assert config.webhook_secret_was_fixed is False


def test_a_long_secret_is_truncated_to_telegrams_limit():
    config = settings(WEBHOOK_SECRET="a" * 400)
    assert len(config.webhook_secret) == 256


# -- the owner ---------------------------------------------------------------- #
def test_owner_username_needs_no_numeric_id():
    config = settings(OWNER_USERNAME="@TechnicslSerena")
    assert config.owner_username == "TechnicslSerena"
    assert config.validate() == []


def test_the_bot_is_open_and_says_so():
    """No owner configured means no diagnostics operator — not "locked to nobody"."""
    config = settings()
    assert config.summary()["public_bot"] is True
    notes = " | ".join(config.warnings())
    assert "open to everyone" in notes


def test_only_the_configured_owner_passes():
    access = Access(settings(OWNER_USER_ID="4242"))
    assert access.is_owner(4242) is True
    assert access.is_owner(4243) is False
    assert access.is_owner(None) is False


def test_an_unconfigured_owner_grants_nobody_access():
    access = Access(settings())
    assert access.is_owner(1) is False
    assert access.is_owner(4242) is False


def test_a_username_is_resolved_through_telegrams_public_api():
    class Chat:
        id = 987654321
        type = "private"

    class Bot:
        def __init__(self):
            self.looked_up = []

        async def get_chat(self, name):
            self.looked_up.append(name)
            return Chat()

    access = Access(settings(OWNER_USERNAME="@TechnicslSerena"))
    bot = Bot()
    assert asyncio.run(access.resolve_owner(bot)) == 987654321
    assert bot.looked_up == ["@TechnicslSerena"]
    assert access.is_owner(987654321) is True


def test_a_typo_in_the_username_warns_instead_of_crashing():
    class Bot:
        async def get_chat(self, name):
            raise TelegramError("Bad Request: chat not found")

    access = Access(settings(OWNER_USERNAME="@NobodyHere"))
    assert asyncio.run(access.resolve_owner(Bot())) == 0
    assert access.is_owner(1) is False  # still nobody, and the bot still runs


# -- the cooldown ------------------------------------------------------------- #
def test_the_cooldown_holds_a_user_back_but_not_the_owner():
    config = settings(USER_COOLDOWN_SECONDS="5", OWNER_USER_ID="1")
    access = Access(config)
    access.mark_request(99)

    assert access.cooldown_remaining(99) > 4
    assert access.cooldown_remaining(1) == 0, "the operator tests their own bot without waiting"
    assert access.cooldown_remaining(100) == 0, "a first-time user waits for nothing"


def test_the_cooldown_can_be_switched_off():
    access = Access(settings(USER_COOLDOWN_SECONDS="0"))
    access.mark_request(99)
    assert access.cooldown_remaining(99) == 0


# -- assets ------------------------------------------------------------------- #
def test_the_shipped_assets_are_present_and_named(tmp_path):
    library = AssetLibrary(cache_path=tmp_path / "ids.json")
    start = library.get(START_IMAGE)
    animation = library.get(DOWNLOAD_ANIMATION)

    assert start is not None and start.exists, "the /start picture must ship with the bot"
    assert animation is not None and animation.exists, "the progress animation must ship too"
    assert library.animation("progress") == animation, "the same file, not a copy"
    assert library.animation("nope") is None


def test_file_ids_are_cached_so_the_images_upload_once(tmp_path):
    cache = tmp_path / "ids.json"
    library = AssetLibrary(cache_path=cache)
    assert library.file_id(START_IMAGE) is None

    library.remember(START_IMAGE, "AgACAgQAAxkBAAI")
    assert json.loads(cache.read_text())[START_IMAGE] == "AgACAgQAAxkBAAI"
    assert AssetLibrary(cache_path=cache).file_id(START_IMAGE) == "AgACAgQAAxkBAAI"
    assert "AgACAgQAAxkBAAI" not in json.dumps(library.describe()), "the panel must not leak ids"


def test_a_read_only_cache_directory_does_not_break_the_bot(tmp_path):
    library = AssetLibrary(cache_path=tmp_path / "nowhere" / "ids.json", directory=tmp_path / "assets")
    library.remember(START_IMAGE, "some-id")  # must not raise
    assert library.file_id(START_IMAGE) == "some-id"


# -- the animated status card ------------------------------------------------- #
class FakeMessage:
    message_id = 5
    animation = None
    text = ""


class FakeBot:
    def __init__(self, *, fail_media: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_media = fail_media

    async def edit_message_text(self, **kwargs):
        self.calls.append("edit_message_text")

    async def edit_message_caption(self, **kwargs):
        self.calls.append("edit_message_caption")

    async def edit_message_media(self, **kwargs):
        self.calls.append("edit_message_media")
        if self.fail_media:
            raise TelegramError("Bad Request: there is no media in the message to edit")

    async def delete_message(self, **kwargs):
        self.calls.append("delete_message")


async def test_an_animated_card_edits_its_caption():
    bot = FakeBot()
    card = ProgressCard(bot, chat_id=1, message=FakeMessage(), interval=0, animated=True)

    await card.update("half way")
    await card.finish("done")

    assert bot.calls == ["edit_message_caption", "edit_message_caption"]


async def test_a_text_card_edits_its_text():
    bot = FakeBot()
    card = ProgressCard(bot, chat_id=1, message=FakeMessage(), interval=0, animated=False)

    await card.update("half way")
    await card.finish("done")

    assert bot.calls == ["edit_message_text", "edit_message_text"]


async def test_finish_only_counts_once():
    bot = FakeBot()
    card = ProgressCard(bot, chat_id=1, message=FakeMessage(), interval=0)

    await card.finish("first")
    await card.finish("second")
    await card.update("late progress")

    assert bot.calls == ["edit_message_text"]


async def test_a_failed_animation_switch_falls_back_to_text():
    """Older clients and odd caches can refuse the media edit; the job must not care."""
    bot = FakeBot(fail_media=True)
    card = ProgressCard(bot, chat_id=1, message=FakeMessage(), interval=0, animated=True)
    asset = AssetLibrary().animation("progress")

    card.show_animation(asset, "⬇️ Downloading", key="download")
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)

    assert card.animated is False, "stop trying to animate; keep the card usable"
    await card.update("still working")
    assert "edit_message_text" in bot.calls


async def test_an_animation_switch_happens_once_per_phase():
    bot = FakeBot()
    card = ProgressCard(bot, chat_id=1, message=FakeMessage(), interval=0, animated=True)
    asset = AssetLibrary().animation("progress")

    card.show_animation(asset, "⬇️ Downloading", key="download")
    card.show_animation(asset, "⬇️ Downloading", key="download")
    await asyncio.sleep(0.05)
    await asyncio.sleep(0.05)

    assert bot.calls.count("edit_message_media") == 1


# -- texts -------------------------------------------------------------------- #
def test_the_welcome_says_nothing_about_engines_limits_commands_or_owners():
    """The operator asked for a short /start: what to send, what comes back, nothing else."""
    text = welcome("YouTube Downloader", "3.3.2", owner="@TechnicslSerena", show_owner=True)
    assert "@TechnicslSerena" in text, "the credit shows when it is switched on"

    plain = welcome("YouTube Downloader", "3.3.2", owner="@TechnicslSerena")
    assert "@TechnicslSerena" not in plain, "off by default: no owner id for strangers"
    for banned in ("TypeTube", "FFmpeg", "MB per file", "/diag", "OWNER", "Commands"):
        assert banned not in plain, f"/start must not mention {banned!r}"
    assert len(plain) < 500


def test_the_start_screen_advertises_every_source_it_accepts():
    text = welcome("YouTube Downloader", "3.3.2")
    assert "YouTube" in text and "Torrent" in text and "Direct" in text


def test_the_diagnostics_panel_shows_the_host_and_the_queue():
    facts = {
        "cpu": "0.1 CPU — a throttled container",
        "mtproto": "ready",
        "cached_tracks": 3,
        "assets": {START_IMAGE: "89 KB, cached"},
        "notes": ["something worth knowing"],
    }
    text = diagnostics(
        settings().summary(),
        {"active": 1, "slots": 2, "completed": 7, "failed": 0, "cancelled": 1, "free_disk": "1.2 GB"},
        facts,
    )
    assert "owner only" in text
    assert "0.1 CPU" in text and "1/2" in text and "1.2 GB" in text
    assert "cached tracks: 3" in text


def test_the_owner_only_refusal_points_at_what_is_open():
    text = owner_only("/diag")
    assert "Owner only" in text
    assert "open to you" in text


@pytest.mark.parametrize("env,expected", [({"RICH_MESSAGES": "false"}, False), ({}, True)])
def test_rich_messages_can_be_switched_off(env, expected):
    assert settings(**env).rich_messages is expected


# -- the /start message -------------------------------------------------------- #
class FakeSentPhoto:
    class Photo:
        file_id = "AgACAgQAAxkBAAI-start"

    photo: ClassVar[list] = [Photo()]


class FakeStartMessage:
    """Just enough of ``telegram.Message`` for the welcome screen."""

    def __init__(self) -> None:
        self.photos: list[dict] = []
        self.texts: list[str] = []

    async def reply_photo(self, **kwargs):
        self.photos.append(kwargs)
        return FakeSentPhoto()

    async def reply_text(self, text: str, **kwargs):
        self.texts.append(text)
        return None


class FakeContext:
    def __init__(self, rt) -> None:
        self.bot_data = {"runtime": rt}


def start_runtime(tmp_path, **env):
    from bot.runtime import Runtime

    config = settings(**env)
    return Runtime(
        settings=config,
        manager=object(),  # never touched by the welcome screen
        assets=AssetLibrary(cache_path=tmp_path / "ids.json"),
        access=Access(config),
        bot_username="mock_bot",
    )


async def _run_start(tmp_path, **env):
    from bot.handlers.commands import CAPTION_LIMIT, MESSAGE_LIMIT, start_command

    rt = start_runtime(tmp_path, **env)
    message = FakeStartMessage()
    update = type("FakeUpdate", (), {"effective_message": message})()
    await start_command(update, FakeContext(rt))
    return message, rt, CAPTION_LIMIT, MESSAGE_LIMIT


async def test_the_start_picture_carries_the_whole_welcome(tmp_path):
    """One picture, one short caption, and no follow-up message full of commands."""
    message, _rt, limit, _message_limit = await _run_start(tmp_path, OWNER_USER_ID="5550001")

    assert len(message.photos) == 1, "the welcome screen is a picture"
    caption = message.photos[0]["caption"]
    assert len(caption) <= limit
    assert "/diag" not in caption, "the operator's tools are not advertised to users"
    assert "Commands" not in caption
    assert message.texts == [], "nothing follows the picture"


async def test_a_username_is_credited_only_when_switched_on(tmp_path):
    message, _rt, _limit, _message_limit = await _run_start(
        tmp_path, OWNER_USERNAME="@TechnicslSerena", SHOW_OWNER_IN_START="true"
    )
    assert "Owner</b>: @TechnicslSerena" in message.photos[0]["caption"]


async def test_no_owner_means_no_credit_line_at_all(tmp_path):
    """A public deployment must not show users a placeholder like 'not configured'."""
    message, _rt, _limit, _message_limit = await _run_start(tmp_path)
    assert "Owner" not in message.photos[0]["caption"]


async def test_a_client_that_refuses_the_picture_still_gets_the_text(tmp_path):
    from bot.handlers.commands import start_command

    class Broken(FakeStartMessage):
        async def reply_photo(self, **kwargs):
            raise TelegramError("Bad Request: chat not found")

    rt = start_runtime(tmp_path, OWNER_USER_ID="5550001")
    message = Broken()
    update = type("FakeUpdate", (), {"effective_message": message})()
    await start_command(update, FakeContext(rt))

    assert message.photos == []
    assert len(message.texts) == 1
    assert len(message.texts[0]) <= 4096


async def test_without_rich_messages_the_whole_screen_is_one_text(tmp_path):
    message, _rt, _limit, message_limit = await _run_start(tmp_path, RICH_MESSAGES="false", OWNER_USER_ID="1")
    assert message.photos == []
    assert len(message.texts) == 1
    assert len(message.texts[0]) <= message_limit


# -- the URL Telegram is told to post to --------------------------------------- #
def test_a_stale_webhook_url_loses_to_the_platform_url():
    """The real incident: the service was renamed on Render and WEBHOOK_URL was left behind.

    Updates went to a host that no longer answers, so the bot looked offline while
    reporting perfect health.
    """
    config = settings(
        WEBHOOK_URL="https://youtube-telegram-bot-x2pn.onrender.com",
        RENDER_EXTERNAL_URL="https://youtube-downloader-for-telegram.onrender.com",
    )

    assert config.webhook_target == "https://youtube-downloader-for-telegram.onrender.com/telegram"
    assert config.webhook_url_source == "RENDER_EXTERNAL_URL"
    assert config.stale_webhook_url == "https://youtube-telegram-bot-x2pn.onrender.com"
    assert any("WEBHOOK_URL is set to" in note for note in config.warnings())


def test_an_agreeing_url_is_left_alone():
    config = settings(WEBHOOK_URL="https://same.example.com/", RENDER_EXTERNAL_URL="https://same.example.com")
    assert config.webhook_url_source == "WEBHOOK_URL"
    assert config.stale_webhook_url == ""
    assert not any("WEBHOOK_URL is set to" in note for note in config.warnings())


def test_without_a_platform_url_the_operator_keeps_their_own(tmp_path):
    config = settings(WEBHOOK_URL="https://my-domain.example.com")
    assert config.webhook_target == "https://my-domain.example.com/telegram"
    assert config.webhook_url_source == "WEBHOOK_URL"


def test_the_diagnostics_panel_flags_the_ignored_url():
    from bot.texts import diagnostics

    config = settings(
        WEBHOOK_URL="https://old.onrender.com",
        RENDER_EXTERNAL_URL="https://new.onrender.com",
        WEBHOOK_SECRET="ok_secret",
    )
    panel = diagnostics(
        config.summary(),
        {"free_disk": "1 GB", "active": 0, "slots": 2, "completed": 0, "failed": 0, "cancelled": 0},
        {"cpu": "2.0 CPU", "mtproto": "idle", "cached_tracks": 0},
    )

    assert "URL from RENDER_EXTERNAL_URL" in panel
    assert "https://old.onrender.com" in panel and "https://new.onrender.com/telegram" in panel
