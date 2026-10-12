#!/usr/bin/env python3
"""End-to-end check of the whole bot, with no Telegram account and no surprises.

What it does:

1. builds a small test video with FFmpeg,
2. copies the engine stub (scripts/e2e_engine_stub.py), which answers ``resolve --json``
   and ``download`` with that video — the real TypeTube contract, without the network,
3. runs a mock Telegram Bot API that records every call,
4. starts this repository's bot against that mock, in webhook mode,
5. injects a YouTube link, presses a quality button, and waits for the upload,
6. asserts the delivered video has dimensions, a duration, a thumbnail, a caption
   and streaming enabled — and that the bot shuts down cleanly on SIGTERM.

Exit code 0 means the download → mux → thumbnail → upload pipeline works end to end.

    python scripts/e2e_local.py                # uses ffmpeg from PATH
    python scripts/e2e_local.py --ffmpeg /usr/bin/ffmpeg --keep-files

If the script is not run from inside the checkout (as the Docker CI job does, from
``/opt/scripts``), point it at the code with ``YT_BOT_ROOT=/app`` or run it with the
checkout as the working directory.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from aiohttp import ClientSession, web


def _find_repo_root() -> Path:
    """Locate the checkout that contains ``main.py``.

    The harness is normally run from the repository, but it is also run from a
    read-only mount inside the Docker image (``/opt/scripts``), where its own
    directory says nothing about where the bot lives. Prefer explicit overrides,
    then the current directory, then the script's own location.
    """
    here = Path(__file__).resolve()
    override = os.getenv("YT_BOT_ROOT")
    candidates = [Path(override)] if override else []
    candidates += [Path.cwd(), *here.parents, here.parent]
    for candidate in candidates:
        if (candidate / "main.py").is_file() and (candidate / "config.py").is_file():
            return candidate
    return here.parents[1]


ROOT = _find_repo_root()
# The torrent step builds a real .torrent with the bot's own encoder, so the checkout has to
# be importable from this process too.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
TOKEN = "123456789:placeholder-token-used-only-by-the-e2e-harness"
#: A real YouTube-shaped link — the bot refuses anything else before it reaches the engine.
LINK = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
#: The title the stub reports for that link.
STUB_TITLE = "E2E Test clip"
WEBHOOK_SECRET = "e2e-local-secret"
CHAT_ID = 5550001
OWNER_USER_ID = 5550001  # the operator: sees /diag, never throttled
USER_ID = 6660002  # an ordinary user of the public bot


def log(message: str) -> None:
    print(f"\033[36m▶\033[0m {message}", flush=True)


def ok(message: str) -> None:
    print(f"  \033[32m✓\033[0m {message}", flush=True)


def fail(message: str) -> None:
    print(f"  \033[31m✗\033[0m {message}", flush=True)


# --------------------------------------------------------------------------- #
# Mock Telegram Bot API
# --------------------------------------------------------------------------- #
class MockBotAPI:
    """Implements just the methods this bot calls, and records all of them."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._message_id = 1000
        self.app = web.Application()
        self.app.router.add_route("*", "/bot{token}/{method}", self._dispatch)
        self.app.router.add_route("*", "/file/bot{token}/{path:.*}", self._serve_file)
        self.app.router.add_get("/_calls", self._dump)
        self.app.router.add_get("/_health", lambda _r: web.json_response({"ok": True}))

    # -- helpers ------------------------------------------------------------
    def next_message_id(self) -> int:
        self._message_id += 1
        return self._message_id

    def calls_of(self, method: str) -> list[dict]:
        return [call for call in self.calls if call["method"] == method]

    async def _serve_file(self, request: web.Request) -> web.Response:
        return web.Response(status=404)

    async def _dump(self, _request: web.Request) -> web.Response:
        return web.json_response(
            [
                {
                    "method": call["method"],
                    "keys": sorted(call["fields"]),
                    "files": call["files"],
                    "content_type": call.get("content_type"),
                }
                for call in self.calls
            ]
        )

    def _message(self, chat_id: int, text: str = "", extra: dict | None = None) -> dict:
        payload = {
            "message_id": self.next_message_id(),
            "date": int(time.time()),
            "chat": {"id": chat_id, "type": "private", "first_name": "E2E"},
            "from": {"id": 4242, "is_bot": True, "first_name": "Mock"},
            "text": text,
        }
        if extra:
            payload.update(extra)
        return payload

    # -- dispatch -----------------------------------------------------------
    async def _dispatch(self, request: web.Request) -> web.Response:
        method = request.match_info["method"]
        fields: dict = {}
        files: list[str] = []

        # python-telegram-bot may send JSON, form-urlencoded or multipart.
        ctype = (request.content_type or "").lower()
        if ctype.startswith(("multipart/", "application/x-www-form-urlencoded")):
            form = await request.post()
            for key, value in form.items():
                if isinstance(value, web.FileField):
                    data = value.file.read()
                    files.append(f"{key}:{len(data)}")
                    fields[f"{key}_name"] = value.filename or ""
                else:
                    fields[key] = value
        else:
            try:
                fields = await request.json()
                if not isinstance(fields, dict):
                    fields = {}
            except Exception as exc:
                fields = {}
                print(f"  (mock: could not parse {method} body as {ctype!r}: {exc})", flush=True)

        record = {"method": method, "fields": fields, "files": files, "content_type": ctype, "at": time.time()}
        self.calls.append(record)

        chat_id = int(fields.get("chat_id", CHAT_ID))
        result: object = True

        if method == "getMe":
            result = {
                "id": 4242,
                "is_bot": True,
                "first_name": "Mock Bot",
                "username": "mock_bot",
                "can_join_groups": True,
                "can_read_all_group_messages": False,
                "supports_inline_queries": False,
            }
        elif method == "sendMessage" or method == "editMessageText":
            result = self._message(chat_id, str(fields.get("text", "")))
        elif method == "sendVideo":
            result = self._message(
                chat_id,
                "",
                {
                    "video": {
                        "file_id": "mock-video",
                        "file_unique_id": "mock-video-u",
                        "width": int(fields.get("width") or 320),
                        "height": int(fields.get("height") or 240),
                        "duration": int(fields.get("duration") or 3),
                        "file_name": str(fields.get("video_name") or "video.mp4"),
                        "mime_type": "video/mp4",
                        "file_size": 1024,
                    },
                    "caption": str(fields.get("caption", "")),
                },
            )
        elif method == "sendAudio":
            result = self._message(
                chat_id,
                "",
                {
                    "audio": {
                        "file_id": "mock-audio",
                        "file_unique_id": "mock-audio-u",
                        "duration": int(fields.get("duration") or 3),
                        "file_name": str(fields.get("audio_name") or "audio.m4a"),
                        "mime_type": "audio/mp4",
                        "file_size": 1024,
                    },
                    "caption": str(fields.get("caption", "")),
                },
            )
        elif method == "sendDocument":
            result = self._message(
                chat_id,
                "",
                {
                    "document": {
                        "file_id": "mock-doc",
                        "file_unique_id": "mock-doc-u",
                        "file_name": str(fields.get("document_name") or "file.bin"),
                        "file_size": 1024,
                    },
                    "caption": str(fields.get("caption", "")),
                },
            )
        elif method == "sendPhoto":
            result = self._message(
                chat_id,
                "",
                {
                    "photo": [
                        {
                            "file_id": "mock-start-image",
                            "file_unique_id": "mock-start-image-u",
                            "width": 1280,
                            "height": 714,
                            "file_size": 90000,
                        }
                    ],
                    "caption": str(fields.get("caption", "")),
                },
            )
        elif method == "sendAnimation":
            result = self._message(
                chat_id,
                "",
                {
                    "animation": {
                        "file_id": "mock-progress-animation",
                        "file_unique_id": "mock-progress-u",
                        "width": 480,
                        "height": 270,
                        "duration": 2,
                        "file_name": str(fields.get("animation_name") or "progress.gif"),
                        "mime_type": "image/gif",
                        "file_size": 460000,
                    },
                    "caption": str(fields.get("caption", "")),
                },
            )
        elif method == "editMessageMedia" or method == "editMessageCaption":
            result = self._message(
                chat_id,
                "",
                {
                    "animation": {
                        "file_id": "mock-progress-animation",
                        "file_unique_id": "mock-progress-u",
                        "width": 480,
                        "height": 270,
                        "duration": 2,
                        "file_name": "progress.gif",
                        "mime_type": "image/gif",
                        "file_size": 460000,
                    },
                    "caption": str(fields.get("caption", "")),
                },
            )
        elif method == "getWebhookInfo":
            result = {"url": "", "has_custom_certificate": False, "pending_update_count": 0}

        return web.json_response({"ok": True, "result": result})


# --------------------------------------------------------------------------- #
# Media fixture
# --------------------------------------------------------------------------- #
def build_test_video(ffmpeg: str, directory: Path) -> Path:
    """A 3-second 320×240 clip with a tone, so muxing and thumbnails are real."""
    target = directory / "clip.mp4"
    attempts = [
        ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
        ["-c:v", "mpeg4"],
    ]
    for codec_args in attempts:
        command = [
            ffmpeg,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-f",
            "lavfi",
            "-i",
            "testsrc=size=320x240:rate=15:duration=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:duration=3",
            *codec_args,
            "-c:a",
            "aac",
            "-shortest",
            "-movflags",
            "+faststart",
            str(target),
        ]
        completed = subprocess.run(command, capture_output=True, text=True)
        if completed.returncode == 0 and target.exists() and target.stat().st_size > 1000:
            return target
        if target.exists():
            target.unlink()
    raise RuntimeError(f"Could not build a test video with {ffmpeg}: {completed.stderr[-400:]}")


def write_engine_stub(directory: Path, source: Path, title: str) -> Path:
    """Put an executable stand-in for the TypeTube CLI into the workspace.

    The stub is a real file in this repository (``scripts/e2e_engine_stub.py``) so it can be
    linted and read; it is copied into the writable workspace and handed the fixture through
    the environment. It speaks the CLI's contract exactly: ``resolve --json``, a ``download``
    that writes a file and reports ``Completed: <path> (<mb> MB in <s>s)``, and progress sent
    with carriage returns — which is what the Python layer really has to cope with.
    """
    origin = ROOT / "scripts" / "e2e_engine_stub.py"
    if not origin.is_file():
        origin = Path(__file__).resolve().parent / "e2e_engine_stub.py"

    target = directory / "typetube-stub"
    shutil.copyfile(origin, target)
    target.chmod(0o755)
    return target


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #
async def wait_for(predicate, *, timeout: float, interval: float = 0.4, what: str = "condition", abort=None):
    """Poll ``predicate`` until it is truthy.

    ``abort`` short-circuits the wait: it is how a dead bot process ends every
    remaining stage immediately instead of letting each timeout expire.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        if abort is not None and abort():
            raise TimeoutError(f"Timed out waiting for {what} (the bot process is gone)")
        await asyncio.sleep(interval)
    raise TimeoutError(f"Timed out waiting for {what}")


async def post_webhook(session: ClientSession, url: str, payload: dict) -> None:
    async with session.post(url, json=payload, headers={"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}) as response:
        await response.read()
        if response.status != 200:
            raise RuntimeError(f"Webhook returned {response.status}")


_COMMAND = re.compile(r"^/[A-Za-z0-9_]{1,32}(?:@[A-Za-z0-9_]{1,32})?$")


def _entities_for(text: str) -> list[dict] | None:
    """What the real Bot API attaches to a message: a bot_command entity on the leading token.

    python-telegram-bot's CommandHandler matches on that entity, not on the raw text, so a
    hand-written update without it is silently dropped by every command handler.
    """
    token = text.split(maxsplit=1)[0] if text else ""
    if _COMMAND.match(token):
        return [{"type": "bot_command", "offset": 0, "length": len(token)}]
    return None


def update_message(text: str, *, user_id: int = USER_ID, update_id: int = 1, message_id: int = 7) -> dict:
    message: dict = {
        "message_id": message_id,
        "date": int(time.time()),
        "chat": {"id": user_id, "type": "private", "first_name": "E2E"},
        "from": {"id": user_id, "is_bot": False, "first_name": "E2E"},
        "text": text,
    }
    entities = _entities_for(text)
    if entities:
        message["entities"] = entities
    return {"update_id": update_id, "message": message}


def write_torrent(workdir: Path, media: Path, base_url: str) -> Path:
    """Build a single-file torrent for the test clip, seeded by an HTTP server.

    No tracker and no peers are involved: the ``url-list`` entry is a BEP-19 web seed, which
    is also how a magnet with no seeders still completes. That keeps this run hermetic —
    nothing leaves the machine.
    """
    from bot.torrent import bencode

    payload = media.read_bytes()
    piece = 1024 * 1024
    hashes = b"".join(
        hashlib.sha1(payload[i : i + piece]).digest()
        for i in range(0, len(payload), piece)
    )
    torrent = bencode(
        {
            "announce": "http://127.0.0.1:1/announce",  # deliberately dead tracker
            "url-list": [f"{base_url}/{media.name}"],
            "info": {
                "name": media.name,
                "piece length": piece,
                "pieces": hashes,
                "length": len(payload),
            },
        }
    )
    path = workdir / "clip.torrent"
    path.write_bytes(torrent)
    return path


def update_callback(data: str, message_id: int, *, user_id: int = USER_ID, update_id: int = 2) -> dict:
    """A button press.

    ``user_id`` matters: every cached choice (a quality, a torrent's file list) is scoped to
    the user who made it, so a button pressed by the wrong account is refused.
    """
    return {
        "update_id": update_id,
        "callback_query": {
            "id": "1",
            "from": {"id": user_id, "is_bot": False, "first_name": "E2E"},
            "chat_instance": "1",
            "data": data,
            "message": {
                "message_id": message_id,
                "date": int(time.time()),
                "chat": {"id": CHAT_ID, "type": "private", "first_name": "E2E"},
                "from": {"id": 4242, "is_bot": True, "first_name": "Mock Bot"},
                "text": "choose",
            },
        },
    }


async def run(args: argparse.Namespace) -> int:
    ffmpeg = args.ffmpeg or shutil.which("ffmpeg")
    if not ffmpeg:
        fail("FFmpeg is required for this check (install it, or pass --ffmpeg)")
        return 2

    aria2 = args.aria2 or shutil.which("aria2c")
    if not aria2:
        fail("aria2c is required for the torrent and direct-link steps (install aria2, or pass --aria2)")
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="ytbot-e2e-"))
    downloads = workdir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    log(f"Workspace: {workdir}")
    log(f"FFmpeg: {ffmpeg}")
    log(f"aria2c: {aria2}")

    session: ClientSession | None = None
    bot_process: subprocess.Popen | None = None
    runners = []

    try:
        # 1. fixture and engine stub -----------------------------------------
        video = build_test_video(ffmpeg, workdir)
        ok(f"test video built ({video.stat().st_size / 1024:.0f} KB, 320×240, 3 s)")
        stub = write_engine_stub(workdir, video, STUB_TITLE)
        ok(f"engine stub ready ({stub.name})")

        # 2a. a fixture server: the file a direct link points at, and the web seed a
        # torrent pulls from. Both steps are hermetic — nothing leaves this machine.
        fixture_root = workdir / "fixtures"
        fixture_root.mkdir(exist_ok=True)
        shutil.copy(video, fixture_root / video.name)
        (fixture_root / "notes.bin").write_bytes(os.urandom(64_000))
        fixtures = web.Application()
        fixtures.router.add_static("/", fixture_root, show_index=True)
        fixture_runner = web.AppRunner(fixtures, access_log=None)
        await fixture_runner.setup()
        fixture_port = args.api_port + 10
        await web.TCPSite(fixture_runner, "127.0.0.1", fixture_port).start()
        runners.append(fixture_runner)
        fixture_base = f"http://127.0.0.1:{fixture_port}"
        ok(f"fixture server on :{fixture_port} ({video.name}, notes.bin)")

        # 2. mock Bot API ----------------------------------------------------
        mock = MockBotAPI()
        api_runner = web.AppRunner(mock.app, access_log=None)
        await api_runner.setup()
        api_site = web.TCPSite(api_runner, "127.0.0.1", args.api_port)
        await api_site.start()
        runners.append(api_runner)

        webhook_url = f"http://127.0.0.1:{args.bot_port}"
        ok(f"mock Bot API on :{args.api_port}, webhook {webhook_url}")

        # 3. the bot ---------------------------------------------------------
        env = {
            **os.environ,
            "BOT_TOKEN": TOKEN,
            "TELEGRAM_API_BASE": f"http://127.0.0.1:{args.api_port}",
            "WEBHOOK_URL": webhook_url,
            "WEBHOOK_SECRET": WEBHOOK_SECRET,
            "PORT": str(args.bot_port),
            "FFMPEG_BINARY": ffmpeg,
            "FFPROBE_BINARY": str(Path(ffmpeg).with_name("ffprobe"))
            if Path(ffmpeg).with_name("ffprobe").exists()
            else "ffprobe",
            "DOWNLOAD_DIR": str(downloads),
            # The engine is the stub above, invoked exactly like the real CLI.
            "TYPETUBE_BIN": str(stub),
            "TYPETUBE_WORKERS": "2",
            "ARIA2_BINARY": aria2,
            "E2E_STUB_SOURCE": str(video),
            "E2E_STUB_TITLE": STUB_TITLE,
            "MAX_CONCURRENT_JOBS": "1",
            "PROGRESS_INTERVAL": "0.5",
            "STARTUP_NOTIFY_CHAT_ID": str(CHAT_ID),
            # The operator: only this id may use /diag, and it is never throttled.
            "OWNER_USER_ID": str(OWNER_USER_ID),
            # Torrent and direct-link steps fetch from a server on 127.0.0.1 in this harness.
            "ALLOW_PRIVATE_LINKS": "true",
            "LOG_LEVEL": os.environ.get("E2E_LOG_LEVEL", "INFO"),
            "LOG_PRETTY": "false",
            "PYTHONUNBUFFERED": "1",
        }
        if args.mtproto_unreachable:
            # Valid-shaped but useless credentials: Pyrogram will try, fail, and must not
            # take the service down with it. This is what a platform deploy looked like
            # when MTProto was awaited before the port was bound.
            env["TELEGRAM_API_ID"] = "1234567"
            env["TELEGRAM_API_HASH"] = "0123456789abcdef0123456789abcdef"

        log_path = workdir / "bot.log"
        log_handle = log_path.open("w")
        bot_process = subprocess.Popen(
            [sys.executable, str(ROOT / "main.py")],
            cwd=str(ROOT),
            env=env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )

        session = ClientSession()

        def bot_is_gone() -> bool:
            return bot_process is not None and bot_process.poll() is not None

        deadline = time.monotonic() + 60
        health = None
        public_health = None
        early_exit: int | None = None
        # Two views of the same endpoint: the platform's shallow probe (no secret) and the
        # operator's detailed one. Both must answer; only one may leak internals.
        operator_headers = {"X-Telegram-Bot-Api-Secret-Token": WEBHOOK_SECRET}
        while time.monotonic() < deadline:
            if bot_process.poll() is not None:
                early_exit = bot_process.returncode
                break
            try:
                async with session.get(f"http://127.0.0.1:{args.bot_port}/health") as response:
                    public_health = await response.json()
                    if public_health.get("ok"):
                        break
            except Exception:
                public_health = None
            await asyncio.sleep(0.5)

        if public_health and public_health.get("ok"):
            ok(f"/health answers the platform's probe: {sorted(public_health)}")
            if "jobs" in public_health or "runtime" in public_health:
                failures.append("the public health payload exposes internals")
                fail("the public health payload exposes internals")
            async with session.get(
                f"http://127.0.0.1:{args.bot_port}/health", headers=operator_headers
            ) as response:
                health = await response.json()

        if early_exit is not None:
            failures.append(f"the bot exited with code {early_exit} before serving /health")
            fail(f"the bot exited with code {early_exit} before serving /health")
            print(f"    (bot root: {ROOT})")
        elif not health or not health.get("ok"):
            failures.append("/health never became ready")
            fail("/health never became ready")
        else:
            ok(
                f"/health ok — ffmpeg={health['runtime']['ffmpeg']}, "
                f"upload capability {health['upload_capability_mb']} MB via {health['upload_transport']}, "
                f"mode {health['mode']}"
            )
            # The harness points TELEGRAM_API_BASE at this mock server, i.e. a *local* Bot API
            # server, whose Telegram-set ceiling is 2000 MB. With --mtproto-unreachable the API
            # ID/hash are set too, so the reported transport is the MTProto route — which may
            # well be "connecting" or "error" at this moment: the port must answer regardless.
            expected_transport = "mtproto" if args.mtproto_unreachable else "bot"
            if health["upload_capability_mb"] != 2000:
                failures.append(f"unexpected upload capability: {health['upload_capability_mb']}")
            if health["upload_transport"] != expected_transport:
                failures.append(f"unexpected transport: {health['upload_transport']}")
            if args.mtproto_unreachable:
                state = health.get("mtproto")
                if state in {"idle", "connecting", "error"}:
                    ok(f"MTProto is not ready (state={state!r}) yet the bot serves /health")
                else:
                    failures.append(f"unexpected MTProto state while unreachable: {state!r}")
                    fail(f"unexpected MTProto state: {state!r}")

        # startup introduction -------------------------------------------------
        intro = await wait_for(
            lambda: [c for c in mock.calls_of("sendMessage") if "Bot online" in str(c["fields"].get("text", ""))],
            timeout=30,
            what="the startup introduction message",
            abort=bot_is_gone,
        )
        if intro:
            ok("startup introduction delivered to the notify chat")
        commands = mock.calls_of("setMyCommands")
        if commands:
            ok("command menu published to Telegram")

        # 3b. /start: picture, shortcuts, owner credit, add-to-group link -------
        log("Sending /start")
        await post_webhook(session, f"{webhook_url}/telegram", update_message("/start", update_id=10, message_id=11))
        # (the ordinary user's own chat is the same numeric id space in this harness)
        started = await wait_for(
            lambda: mock.calls_of("sendPhoto"),
            timeout=30,
            what="the /start picture",
            abort=bot_is_gone,
        )
        caption = str(started[-1]["fields"].get("caption", ""))
        ok(f"/start sent a real picture ({started[-1]['files']}) with a caption")
        keyboard_markup = str(started[-1]["fields"].get("reply_markup", ""))
        if "startgroup" not in keyboard_markup:
            failures.append("the /start keyboard is missing the add-to-group link")
            fail("add-to-group link missing from /start")
        else:
            ok("the /start keyboard offers adding the bot to a group")
        # The welcome screen is deliberately short: no owner id, no engine name, no size
        # ceilings, no command dump — and it advertises every source the bot accepts.
        banned = ["Owner", "TypeTube", "FFmpeg", "MB per file", "/diag", "Commands"]
        leaked = [word for word in banned if word in caption]
        if leaked:
            failures.append(f"the /start caption mentions {leaked}")
            fail(f"/start mentions {leaked}")
        else:
            ok("the /start caption stays out of engines, limits, commands and the owner")
        for kind in ("YouTube", "Torrent", "Direct"):
            if kind not in caption:
                failures.append(f"the /start caption does not mention {kind}")
                fail(f"/start does not advertise {kind}")
        else:
            ok("the /start caption advertises YouTube, torrents and direct links")
        if len(caption) > 1024:
            failures.append("the /start caption is longer than Telegram allows")
            fail("the /start caption exceeds 1024 characters")

        # 3c. /diag is owner-only --------------------------------------------
        log(f"Sending /diag as a normal user ({USER_ID})")
        await post_webhook(session, f"{webhook_url}/telegram", update_message("/diag", update_id=11, message_id=12))
        refusal = await wait_for(
            lambda: [
                call
                for call in mock.calls_of("sendMessage")
                if "Owner only" in str(call["fields"].get("text", ""))
            ],
            timeout=30,
            what="the owner-only refusal",
            abort=bot_is_gone,
        )
        ok(f"/diag is refused for a normal user: {str(refusal[-1]['fields']['text'])[:48]!r}")

        log(f"Sending /diag as the owner ({OWNER_USER_ID})")
        await post_webhook(
            session,
            f"{webhook_url}/telegram",
            update_message("/diag", user_id=OWNER_USER_ID, update_id=12, message_id=13),
        )
        panel = await wait_for(
            lambda: [
                call
                for call in mock.calls_of("sendMessage")
                if "Diagnostics" in str(call["fields"].get("text", ""))
            ],
            timeout=30,
            what="the owner's diagnostics panel",
            abort=bot_is_gone,
        )
        panel_text = str(panel[-1]["fields"]["text"])
        ok("the owner sees the diagnostics panel")
        for expected in ("CPU budget", "free disk", "transport", "delivered"):
            if expected not in panel_text:
                failures.append(f"the diagnostics panel is missing {expected!r}")
                fail(f"diagnostics panel missing {expected!r}")

        # 3d. the fair-use cooldown ------------------------------------------
        log("Sending two links back to back as a normal user")
        await post_webhook(session, f"{webhook_url}/telegram", update_message(LINK, update_id=13, message_id=14))
        await post_webhook(session, f"{webhook_url}/telegram", update_message(LINK, update_id=14, message_id=15))
        await wait_for(
            lambda: [
                call
                for call in mock.calls_of("sendMessage")
                if "One moment" in str(call["fields"].get("text", ""))
            ],
            timeout=30,
            what="the cooldown notice",
            abort=bot_is_gone,
        )
        ok("the second link inside the fairness delay is answered with a cooldown notice")

        # 4. send a link ------------------------------------------------------
        log(f"Sending a link: {LINK}")
        mark = len(mock.calls)
        await post_webhook(
            session,
            f"{webhook_url}/telegram",
            update_message(LINK, user_id=OWNER_USER_ID, update_id=15, message_id=16),
        )

        keyboard_calls = await wait_for(
            lambda: [
                call
                for call in mock.calls[mark:]
                if call["method"] in ("sendMessage", "editMessageText") and "dl:" in str(call["fields"])
            ],
            timeout=90,
            what="the quality keyboard",
            abort=bot_is_gone,
        )
        parsed = json.loads(str(keyboard_calls[-1]["fields"].get("reply_markup")))
        buttons = [button for row in parsed["inline_keyboard"] for button in row]
        labels = [button["text"] for button in buttons]
        callback_data = [button["callback_data"] for button in buttons if button["callback_data"].startswith("dl:")]
        ok(f"quality picker offered: {', '.join(labels)}")
        if not callback_data:
            failures.append("no quality buttons were offered")
            raise RuntimeError("no quality buttons")

        # 5. press the first quality button -----------------------------------
        log(f"Pressing “{buttons[0]['text']}”")
        await post_webhook(
            session,
            f"{webhook_url}/telegram",
            update_callback(callback_data[0], 2001, user_id=OWNER_USER_ID, update_id=21),
        )

        delivered = await wait_for(
            lambda: [c for c in mock.calls if c["method"] in ("sendVideo", "sendAudio", "sendDocument")],
            timeout=180,
            what="the uploaded file",
            abort=bot_is_gone,
        )
        call = delivered[0]
        ok(f"delivered with {call['method']}")

        animations = mock.calls_of("sendAnimation")
        if animations:
            ok(f"the status card is animated ({animations[0]['files']})")
            media_edits = mock.calls_of("editMessageMedia")
            if media_edits:
                ok("the card switched animation when the upload phase started")
        else:
            failures.append("the status card was not animated")
            fail("no animated status card")

        # 6. assertions -------------------------------------------------------
        def check(condition: bool, description: str) -> None:
            if condition:
                ok(description)
            else:
                fail(description)
                failures.append(description)

        fields = call["fields"]
        check(bool(fields.get("caption")), f"caption present: {str(fields.get('caption'))[:70]!r}")
        check("thumbnail" in " ".join(call["files"]), "thumbnail attached to the upload")

        if call["method"] == "sendVideo":
            check(
                bool(fields.get("width")) and bool(fields.get("height")),
                f"dimensions sent ({fields.get('width')}×{fields.get('height')})",
            )
            check(bool(fields.get("duration")), f"duration sent ({fields.get('duration')}s)")
            check(
                str(fields.get("supports_streaming")).lower() in ("true", "1"),
                "streaming enabled (supports_streaming=true)",
            )
            uploaded = [f for f in call["files"] if f.startswith("video:")]
            check(
                bool(uploaded) and int(uploaded[0].split(":")[1]) > 1000,
                f"video bytes uploaded ({uploaded[0].split(':')[1] if uploaded else 0} bytes)",
            )

        check(bool(mock.calls_of("deleteMessage")), "status message cleaned up after delivery")

        # The status card must carry real numbers: a bar, a speed and — when the total size
        # is known — an ETA. Both are read from the engine's own output, never invented.
        captions = [
            str(call["fields"].get("caption", ""))
            for call in mock.calls
            if call["method"] in ("editMessageCaption", "editMessageText")
        ]
        live = [text for text in captions if "⚡" in text]
        check(bool(live), "the status card reports a live speed")
        check(
            any("⏳ ETA" in text for text in captions),
            "the status card reports an ETA once the total size is known",
        )
        check(
            any("📦" in text and "/" in text for text in captions),
            "the status card shows progress against the total size",
        )

        # 6b. a direct link -------------------------------------------------- #
        log("Sending a direct file link")
        await post_webhook(
            session,
            f"{webhook_url}/telegram",
            update_message(
                f"{fixture_base}/notes.bin", user_id=OWNER_USER_ID, update_id=17, message_id=18
            ),
        )
        confirm = await wait_for(
            lambda: [
                call
                for call in mock.calls
                if call["method"] == "editMessageText"
                and "Direct download" in str(call["fields"].get("text", ""))
            ],
            timeout=60,
            what="the direct-link confirmation",
            abort=bot_is_gone,
        )
        markup = json.loads(str(confirm[-1]["fields"].get("reply_markup")))
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        direct_buttons = [b for b in buttons if b["callback_data"].startswith("dd:")]
        if not direct_buttons:
            failures.append("the direct link offered no download button")
            fail("the direct link offered no download button")
        else:
            confirmed = str(confirm[-1]["fields"]["text"])
            announced = [line for line in confirmed.splitlines() if line.startswith("📦")]
            ok(f"direct link confirmed with the size it announced ({announced[0] if announced else 'none'})")
            before = len(mock.calls)
            await post_webhook(
                session,
                f"{webhook_url}/telegram",
                update_callback(direct_buttons[0]["callback_data"], 2001, user_id=OWNER_USER_ID, update_id=22),
            )
            downloaded = await wait_for(
                lambda: [
                    call
                    for call in mock.calls[before:]
                    if call["method"] in ("sendDocument", "sendVideo", "sendAudio")
                ],
                timeout=180,
                what="the direct-downloaded file",
                abort=bot_is_gone,
            )
            call = downloaded[0]
            if call["method"] != "sendDocument":
                failures.append(f"a .bin file should arrive as a document, not {call['method']}")
                fail(f"a .bin file arrived as {call['method']}")
            else:
                uploaded = [f for f in call["files"] if f.startswith("document:")]
                if uploaded:
                    ok(f"direct link delivered as a document ({uploaded[0].split(':')[1]} bytes)")
                else:
                    failures.append("the direct download uploaded no bytes")
                    fail("the direct download uploaded no bytes")

        # 6c. a torrent ------------------------------------------------------ #
        # The .torrent must be served by the fixture server, since the bot fetches it by URL.
        torrent_file = write_torrent(fixture_root, video, fixture_base)
        log(f"Sending a torrent URL ({torrent_file.name})")
        await post_webhook(
            session,
            f"{webhook_url}/telegram",
            update_message(f"{fixture_base}/{torrent_file.name}", user_id=OWNER_USER_ID, update_id=19, message_id=20),
        )
        picker = await wait_for(
            lambda: [
                call
                for call in mock.calls
                if call["method"] == "editMessageText" and "tf:" in str(call["fields"].get("reply_markup"))
            ],
            timeout=120,
            what="the torrent file list",
            abort=bot_is_gone,
        )
        markup = json.loads(str(picker[-1]["fields"]["reply_markup"]))
        buttons = [button for row in markup["inline_keyboard"] for button in row]
        torrent_data = [b for b in buttons if b["callback_data"].startswith("tf:")]
        if not torrent_data:
            failures.append("the torrent offered no files")
            fail("the torrent offered no files")
        else:
            ok(f"torrent listed its file(s): {', '.join(b['text'] for b in buttons[:3])}")
            before = len(mock.calls)
            await post_webhook(
                session,
                f"{webhook_url}/telegram",
                update_callback(torrent_data[0]["callback_data"], 2001, user_id=OWNER_USER_ID, update_id=23),
            )
            fetched = await wait_for(
                lambda: [
                    call
                    for call in mock.calls[before:]
                    if call["method"] in ("sendVideo", "sendDocument", "sendAudio")
                ],
                timeout=300,
                what="the torrent file",
                abort=bot_is_gone,
            )
            call = fetched[0]
            uploaded = [
                f for f in call["files"] if f.split(":")[0] in ("video", "document", "audio")
            ]
            bytes_sent = int(uploaded[0].split(":")[1]) if uploaded else 0
            check_torrent = bytes_sent == video.stat().st_size
            if check_torrent:
                ok(f"torrent file downloaded and delivered ({call['method']}, {bytes_sent} bytes)")
            else:
                failures.append(
                    f"the torrent delivered {bytes_sent} bytes, expected {video.stat().st_size}"
                )
                fail(f"torrent size mismatch: {bytes_sent} != {video.stat().st_size}")

        # 7. graceful shutdown ------------------------------------------------
        log("Sending SIGTERM")
        bot_process.send_signal(signal.SIGTERM)
        try:
            bot_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            bot_process.kill()
            failures.append("the bot did not exit within 30s of SIGTERM")

        # Telegram allows about one edit per second per chat. The floor is 5 s by default,
        # so a job that ran for T seconds must not have produced anything close to T edits.
        edits = len([c for c in mock.calls if c["method"] in ("editMessageCaption", "editMessageText")])
        check(edits < 40, f"status edits stayed throttled ({edits} edits for the whole run)")

        log_text = log_path.read_text(errors="replace")
        check("Stopped. Goodbye." in log_text, "graceful shutdown logged")
        check("Traceback" not in log_text, "no unhandled exception in the log")

        # Telegram's own hard limits: a caption is 1024 characters, a text message 4096.
        # A mock accepts anything, so check them here — production would reject them.
        over_caption = [
            call["method"]
            for call in mock.calls
            if len(str(call["fields"].get("caption", ""))) > 1024
        ]
        over_text = [
            call["method"] for call in mock.calls if len(str(call["fields"].get("text", ""))) > 4096
        ]
        check(
            not over_caption,
            "every caption is inside Telegram's 1024-character limit"
            + (f" — offending calls: {over_caption}" if over_caption else ""),
        )
        check(
            not over_text,
            "every message is inside Telegram's 4096-character limit"
            + (f" — offending calls: {over_text}" if over_text else ""),
        )

        if args.mtproto_unreachable:
            # The deploy failure in one assertion: the port is bound *before* the MTProto
            # client is even contacted, so a slow handshake can never fail a health check.
            bound = log_text.find("HTTP server listening")
            warm_up = log_text.find("MTProto warm-up scheduled")
            check(bound != -1, "the startup log shows the port being bound")
            check(
                bound != -1 and warm_up != -1 and bound < warm_up,
                "the port is bound before the MTProto connection is attempted",
            )
            check("MTProto" in log_text, "the unreachable transport is reported in the log")
            check(
                "Traceback" not in log_text and "Shutting down" in log_text,
                "an unreachable MTProto transport never crashes the service",
            )

        leftovers = [p for p in downloads.glob("job-*") if p.is_dir()]
        check(not leftovers, "temporary download directories removed")

        if args.keep_files:
            log(f"Log kept at {log_path}")
        else:
            log("Log excerpt:")
            for line in log_text.splitlines()[-6:]:
                print(f"    {line}")

    except Exception as exc:
        failures.append(f"{type(exc).__name__}: {exc}")
        fail(str(exc))
    finally:
        if failures:
            print("\n  mock Bot API saw these calls:")
            for call in mock.calls:
                keys = ",".join(sorted(call["fields"]))[:110]
                print(f"    {call['method']:<22} [{call.get('content_type')}] {keys} {call['files']}")
        if bot_process and bot_process.poll() is None:
            bot_process.send_signal(signal.SIGTERM)
            try:
                bot_process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                bot_process.kill()
        if session is not None:
            await session.close()
        for runner in runners:
            await runner.cleanup()
        if not args.keep_files:
            shutil.rmtree(workdir, ignore_errors=True)
        else:
            log(f"Workspace kept at {workdir}")

    print()
    if failures:
        print(f"\033[31m✗ FAILED\033[0m — {len(failures)} problem(s):")
        for item in failures:
            print(f"  • {item}")
        return 1
    print("\033[32m✅ PASS\033[0m — download → mux → thumbnail → upload works end to end.")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Local end-to-end check (no Telegram account needed)")
    parser.add_argument("--ffmpeg", default=os.getenv("FFMPEG_BINARY", ""), help="path to the ffmpeg binary")
    parser.add_argument(
        "--aria2",
        default=os.getenv("ARIA2_BINARY", ""),
        help="path to the aria2c binary (torrents and direct links)",
    )
    parser.add_argument("--api-port", type=int, default=8091, help="port for the mock Bot API")
    parser.add_argument("--bot-port", type=int, default=8093, help="port the bot's webhook server listens on")
    parser.add_argument("--keep-files", action="store_true", help="keep the workspace and logs")
    parser.add_argument(
        "--mtproto-unreachable",
        action="store_true",
        help=(
            "run the bot with MTProto credentials that cannot connect, and require it to boot, "
            "answer /health and deliver normally anyway (the deploy-failure regression)"
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
