"""Startup order — the deploy-blocking bug this file exists to prevent.

A web-service platform (Render, Voroa, Fly) kills a deploy whose health check does not
answer in time. A real deploy failed that way: the MTProto client was awaited *before* the
HTTP port was bound, took over a minute to negotiate with Telegram's data centres, and the
platform gave up on a bot that was perfectly healthy.

These tests pin the contract instead of the incident:

* the port is bound before Telegram is contacted at all,
* a hanging MTProto connection cannot delay readiness,
* a failing MTProto connection is retried, logged, and never fatal,
* ``/health`` answers while MTProto is still connecting.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from telegram.error import BadRequest

from bot import app as app_module
from bot import runner as runner_module
from bot.runtime import BOT_DATA_KEY, Runtime
from bot.web import _FFMPEG_CACHE, create_web_app
from config import load_settings

TOKEN = "123456789:" + "A" * 35

SETTINGS = load_settings(
    {
        "BOT_TOKEN": TOKEN,
        "TELEGRAM_API_ID": "1234567",
        "TELEGRAM_API_HASH": "0123456789abcdef0123456789abcdef",
        "WEBHOOK_URL": "https://bot.example.com",
        "WEBHOOK_SECRET": "secret_for_the_probe_and_the_operator",
    }
)


#: A fresh CI runner can have a monotonically-clocked uptime shorter than the cache TTL, so
#: "checked_at = 0.0" alone is not enough to force a re-probe: clear the cached value too.
def _forget_ffmpeg_cache() -> None:
    _FFMPEG_CACHE["version"] = None
    _FFMPEG_CACHE["checked_at"] = 0.0


class HangingUploader:
    """Credentials are fine, but the connection never completes."""

    ready = True
    state = "connecting"
    last_error = ""

    def __init__(self) -> None:
        self.started = 0
        self.stopped = 0
        self.callback = None  # type: ignore[assignment]

    def reason(self) -> str:
        return ""

    async def start(self) -> bool:
        self.started += 1
        await asyncio.Event().wait()  # never returns — like a stalled DC handshake
        return True

    async def identity(self) -> dict:
        return {"id": 1, "username": "bot", "name": "bot", "capability_mb": 2000}

    async def stop(self) -> None:
        self.stopped += 1


class FlakyUploader(HangingUploader):
    """Fails a fixed number of times, then connects."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.failures = failures

    async def start(self) -> bool:
        self.started += 1
        if self.started <= self.failures:
            raise RuntimeError(f"FloodWait-style failure #{self.started}")
        self.state = "ready"
        return True

    async def identity(self) -> dict:
        return {"id": 1, "username": "bot", "name": "bot", "capability_mb": 2000}


class FakeApplication:
    """Records the calls run_bot makes, so the startup order can be asserted."""

    def __init__(self, runtime: Runtime, order: list[str]) -> None:
        self.bot_data = {BOT_DATA_KEY: runtime}
        self.updater = self
        self._order = order

    async def initialize(self) -> None:
        self._order.append("initialize")

    async def start(self) -> None:
        self._order.append("start")

    async def start_polling(self, **kwargs) -> None:
        self._order.append("polling")

    async def stop(self) -> None:
        pass

    async def shutdown(self) -> None:
        pass


@pytest.fixture
def harness(monkeypatch):
    """A run_bot() with every I/O boundary replaced, recording the startup order."""
    order: list[str] = []
    state: dict = {}

    def build(settings):  # run_bot calls build_application(settings)
        runtime = Runtime(settings=settings, manager=None, uploader=state["uploader"])
        state["runtime"] = runtime
        return FakeApplication(runtime, order)

    async def fake_web(application, settings):
        order.append("web")
        return None

    async def fake_webhook(application, settings):
        order.append("webhook")
        return state.get("webhook_accepted", True)

    async def fake_startup(application):
        order.append("startup")

    async def fake_shutdown(application):
        order.append("shutdown")

    def fake_signals(stop_event):
        # Stop as soon as startup finished, so the test never waits on real signals.
        asyncio.get_running_loop().call_soon(stop_event.set)

    def fake_start_uploader(runtime, **kwargs):
        order.append("uploader")
        app_module.start_uploader(runtime, **kwargs)

    monkeypatch.setattr(runner_module, "build_application", build)
    monkeypatch.setattr(runner_module, "_start_web_server", fake_web)
    monkeypatch.setattr(runner_module, "_register_webhook", fake_webhook)
    monkeypatch.setattr(runner_module, "on_startup", fake_startup)
    monkeypatch.setattr(runner_module, "on_shutdown", fake_shutdown)
    monkeypatch.setattr(runner_module, "_install_signal_handlers", fake_signals)
    monkeypatch.setattr(runner_module, "start_uploader", fake_start_uploader)

    def select(uploader) -> None:
        state["uploader"] = uploader

    def refuse_webhook() -> None:
        state["webhook_accepted"] = False

    return {"order": order, "state": state, "select": select, "refuse_webhook": refuse_webhook}


async def test_the_port_is_bound_before_telegram_is_touched(harness):
    harness["select"](HangingUploader())

    await asyncio.wait_for(runner_module.run_bot(SETTINGS), timeout=5)

    order = harness["order"]
    assert order[0] == "web", "the health check must be answerable first"
    assert order.index("web") < order.index("start")
    assert order.index("web") < order.index("startup")
    assert order.index("web") < order.index("uploader")
    assert order[-1] == "shutdown"


async def test_a_stalled_mtproto_connection_cannot_delay_startup(harness):
    """The deploy-failure regression: MTProto negotiates for minutes; the bot is up."""
    uploader = HangingUploader()
    harness["select"](uploader)

    await asyncio.wait_for(runner_module.run_bot(SETTINGS), timeout=5)

    assert harness["order"][0] == "web"
    assert "uploader" in harness["order"], "the warm-up is still scheduled — just not awaited"
    assert uploader.started == 1, "the connection attempt itself happened"
    runtime: Runtime = harness["state"]["runtime"]
    assert not runtime.background_tasks, "the pending warm-up was cancelled on shutdown"


async def test_the_connection_is_retried_and_never_fatal():
    """Two failures, then success — the bot never notices and nothing is raised."""
    uploader = FlakyUploader(failures=2)
    runtime = Runtime(settings=SETTINGS, manager=None, uploader=uploader)

    app_module.start_uploader(runtime, attempts=3, delay=0.0)
    await asyncio.gather(*list(runtime.background_tasks))

    assert uploader.started == 3, "two failures then a success"
    assert uploader.state == "ready"


async def test_giving_up_is_logged_and_leaves_the_bot_working(caplog):
    uploader = FlakyUploader(failures=99)
    runtime = Runtime(settings=SETTINGS, manager=None, uploader=uploader)

    with caplog.at_level("WARNING"):
        await asyncio.wait_for(app_module._connect_uploader(runtime, attempts=3, delay=0.0), timeout=5)

    assert uploader.started == 3
    assert any("stay on the Bot API" in record.message for record in caplog.records)
    assert uploader.stopped == 0, "a failed connect must not take the bot down"


async def test_a_polling_deployment_still_serves_health(harness):
    """Polling needs no inbound port, but the platform's health check still gets one."""
    polling = load_settings({"BOT_TOKEN": TOKEN, "USE_POLLING": "true", "PORT": "8099"})
    harness["select"](HangingUploader())

    await asyncio.wait_for(runner_module.run_bot(polling), timeout=5)

    order = harness["order"]
    assert order[0] == "web"
    assert "webhook" not in order, "polling must not register a webhook"
    assert "uploader" in order


class FakeRequest:
    """Just the header lookup aiohttp's Request offers."""

    def __init__(self, headers: dict | None = None) -> None:
        self.headers = headers or {}


def _health_route(application, settings):
    web_app = create_web_app(application, settings)  # type: ignore[arg-type]
    return next(r for r in web_app.router.routes() if r.resource.canonical == "/health")


async def test_health_answers_a_public_probe_with_a_shallow_body(monkeypatch):
    """The platform's probe must succeed — and must not learn the server's internals."""
    uploader = HangingUploader()
    runtime = Runtime(settings=SETTINGS, manager=_NullManager(), uploader=uploader)
    application = FakeApplication(runtime, [])

    monkeypatch.setattr("bot.web.ffmpeg_version", _fake_ffmpeg_version)
    _forget_ffmpeg_cache()

    response = await _health_route(application, SETTINGS).handler(FakeRequest())
    payload = json.loads(response.text)

    assert response.status == 200, "a connecting transport must not fail the health check"
    assert payload["ok"] is True
    assert payload["version"]
    assert "mtproto" not in payload, "internals are for the operator, not for the world"
    assert "jobs" not in payload


async def test_health_shows_the_transport_state_to_the_operator(monkeypatch):
    uploader = HangingUploader()
    runtime = Runtime(settings=SETTINGS, manager=_NullManager(), uploader=uploader)
    application = FakeApplication(runtime, [])

    monkeypatch.setattr("bot.web.ffmpeg_version", _fake_ffmpeg_version)
    _forget_ffmpeg_cache()

    request = FakeRequest({"X-Telegram-Bot-Api-Secret-Token": SETTINGS.webhook_secret})
    response = await _health_route(application, SETTINGS).handler(request)
    payload = json.loads(response.text)

    assert payload["mtproto"] == "connecting"
    assert payload["upload_capability_mb"] == 2000
    assert payload["jobs"]["active"] == 0


async def test_health_does_not_spawn_ffmpeg_on_every_poll(monkeypatch):
    calls = {"n": 0}

    async def counting_ffmpeg(settings):
        calls["n"] += 1
        return "7.0.2"

    monkeypatch.setattr("bot.web.ffmpeg_version", counting_ffmpeg)
    _forget_ffmpeg_cache()

    runtime = Runtime(settings=SETTINGS, manager=_NullManager(), uploader=HangingUploader())
    route = _health_route(FakeApplication(runtime, []), SETTINGS)

    for _ in range(5):
        await route.handler(FakeRequest())

    assert calls["n"] == 1, "`ffmpeg -version` is a subprocess; cache it"


async def _fake_ffmpeg_version(settings) -> str:  # pragma: no cover - helper for monkeypatch
    return "7.0.2"


class _NullManager:
    """Just enough JobManager for the health payload."""

    def stats(self, settings) -> dict:
        return {"active": 0}

    def snapshot(self) -> list:
        return []


# -- webhook registration is never reported as a success it was not ------------ #
class WebhookBot:
    def __init__(self, *, fail: str = "") -> None:
        self.fail = fail
        self.registered: list[dict] = []

    async def set_webhook(self, **kwargs):
        self.registered.append(kwargs)
        if self.fail:
            raise BadRequest("Bad Request: " + self.fail)


def _webhook_app(bot):
    class Application:
        pass

    application = Application()
    application.bot = bot
    return application


async def test_a_registration_that_telegram_refuses_is_not_a_success(caplog):
    bot = WebhookBot(fail="secret token contains illegal characters")
    with caplog.at_level("ERROR"):
        registered = await runner_module._register_webhook(_webhook_app(bot), SETTINGS)

    assert registered is False
    assert bot.registered, "the attempt was made"
    assert not any("Webhook registered" in record.message for record in caplog.records)
    assert any("refused the webhook" in record.message.lower() for record in caplog.records)
    assert any("A-Z a-z 0-9 _ -" in record.getMessage() for record in caplog.records)


async def test_a_registration_telegram_accepts_reports_success(caplog):
    bot = WebhookBot()
    with caplog.at_level("INFO"):
        registered = await runner_module._register_webhook(_webhook_app(bot), SETTINGS)

    assert registered is True
    assert bot.registered[0]["url"] == SETTINGS.webhook_target
    assert any("Webhook registered at" in record.message for record in caplog.records)


async def test_a_refused_webhook_falls_back_to_polling(harness):
    """A bot Telegram cannot reach by webhook must still receive updates."""
    harness["select"](HangingUploader())
    harness["refuse_webhook"]()

    await asyncio.wait_for(runner_module.run_bot(SETTINGS), timeout=5)

    order = harness["order"]
    assert "webhook" in order and "polling" in order, "the refusal must not leave the bot deaf"
    assert order.index("webhook") < order.index("polling")
    assert order[-1] == "shutdown"
