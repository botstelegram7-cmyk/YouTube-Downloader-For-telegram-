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
import json
import os
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
TOKEN = "123456789:placeholder-token-used-only-by-the-e2e-harness"
#: A real YouTube-shaped link — the bot refuses anything else before it reaches the engine.
LINK = "https://www.youtube.com/watch?v=jNQXAC9IVRw"
#: The title the stub reports for that link.
STUB_TITLE = "E2E Test clip"
WEBHOOK_SECRET = "e2e-local-secret"
CHAT_ID = 5550001
USER_ID = 5550001


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


def update_message(text: str) -> dict:
    return {
        "update_id": 1,
        "message": {
            "message_id": 7,
            "date": int(time.time()),
            "chat": {"id": CHAT_ID, "type": "private", "first_name": "E2E"},
            "from": {"id": USER_ID, "is_bot": False, "first_name": "E2E"},
            "text": text,
        },
    }


def update_callback(data: str, message_id: int) -> dict:
    return {
        "update_id": 2,
        "callback_query": {
            "id": "1",
            "from": {"id": USER_ID, "is_bot": False, "first_name": "E2E"},
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

    workdir = Path(tempfile.mkdtemp(prefix="ytbot-e2e-"))
    downloads = workdir / "downloads"
    downloads.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []

    log(f"Workspace: {workdir}")
    log(f"FFmpeg: {ffmpeg}")

    session: ClientSession | None = None
    bot_process: subprocess.Popen | None = None
    runners = []

    try:
        # 1. fixture and engine stub -----------------------------------------
        video = build_test_video(ffmpeg, workdir)
        ok(f"test video built ({video.stat().st_size / 1024:.0f} KB, 320×240, 3 s)")
        stub = write_engine_stub(workdir, video, STUB_TITLE)
        ok(f"engine stub ready ({stub.name})")

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
            "E2E_STUB_SOURCE": str(video),
            "E2E_STUB_TITLE": STUB_TITLE,
            "MAX_CONCURRENT_JOBS": "1",
            "PROGRESS_INTERVAL": "0.5",
            "STARTUP_NOTIFY_CHAT_ID": str(CHAT_ID),
            "LOG_LEVEL": "INFO",
            "LOG_PRETTY": "false",
            "PYTHONUNBUFFERED": "1",
        }
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
        early_exit: int | None = None
        while time.monotonic() < deadline:
            if bot_process.poll() is not None:
                early_exit = bot_process.returncode
                break
            try:
                async with session.get(f"http://127.0.0.1:{args.bot_port}/health") as response:
                    health = await response.json()
                    if health.get("ok"):
                        break
            except Exception:
                health = None
            await asyncio.sleep(0.5)

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
            # server, whose Telegram-set ceiling is 2000 MB. No API ID/hash is set here, so the
            # transport stays "bot" — the point being that the app itself never lowers either.
            if health["upload_capability_mb"] != 2000:
                failures.append(f"unexpected upload capability: {health['upload_capability_mb']}")
            if health["upload_transport"] != "bot":
                failures.append(f"unexpected transport: {health['upload_transport']}")

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

        # 4. send a link ------------------------------------------------------
        log(f"Sending a link: {LINK}")
        await post_webhook(session, f"{webhook_url}/telegram", update_message(LINK))

        keyboard_calls = await wait_for(
            lambda: [
                call
                for call in mock.calls
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
        await post_webhook(session, f"{webhook_url}/telegram", update_callback(callback_data[0], 2001))

        delivered = await wait_for(
            lambda: [c for c in mock.calls if c["method"] in ("sendVideo", "sendAudio", "sendDocument")],
            timeout=180,
            what="the uploaded file",
            abort=bot_is_gone,
        )
        call = delivered[0]
        ok(f"delivered with {call['method']}")

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

        # 7. graceful shutdown ------------------------------------------------
        log("Sending SIGTERM")
        bot_process.send_signal(signal.SIGTERM)
        try:
            bot_process.wait(timeout=30)
        except subprocess.TimeoutExpired:
            bot_process.kill()
            failures.append("the bot did not exit within 30s of SIGTERM")

        log_text = log_path.read_text(errors="replace")
        check("Stopped. Goodbye." in log_text, "graceful shutdown logged")
        check("Traceback" not in log_text, "no unhandled exception in the log")

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
    parser.add_argument("--api-port", type=int, default=8091, help="port for the mock Bot API")
    parser.add_argument("--bot-port", type=int, default=8093, help="port the bot's webhook server listens on")
    parser.add_argument("--keep-files", action="store_true", help="keep the workspace and logs")
    return parser.parse_args()


if __name__ == "__main__":
    sys.exit(asyncio.run(run(parse_args())))
