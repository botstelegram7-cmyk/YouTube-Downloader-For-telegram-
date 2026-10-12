# Repository guide for coding agents

This repository is a **Python** Telegram bot that downloads media with the TypeTube CLI (Node.js) and FFmpeg.
Version 3.0.0 removed the Serverless handlers and the Node worker; nothing here is
JavaScript any more.

## Project layout

- `main.py` — entry point and CLI (`--check`, `--online`, `--polling`, `--webhook`,
  `--print-config`, `--version`). Keep it thin: it parses arguments, then calls the runner.
- `config.py` — the entire configuration surface as a frozen `Settings` dataclass. Every
  value has a default, is validated with a readable error, and is documented in
  `.env.example`, `README.md` and `docs/DEPLOYMENT.md`. `config.py` must never import the
  `bot` package (that is what makes `main.py --check` dependency-free).
- `bot/runner.py` — process lifecycle only: initialize → on_startup → start → web server +
  webhook (or polling) → SIGTERM → graceful shutdown.
- `bot/app.py` — builds the PTB `Application`: handlers, timeouts, startup introduction,
  command menu.
- `bot/web.py` — the aiohttp app: `/`, `/health`(+`/healthz`), `/stats`,
  `POST /<WEBHOOK_PATH>`.
- `bot/handlers/` — thin adapters (`commands.py`, `links.py`, `callbacks.py`, `errors.py`).
  Logic lives in the modules below, not here.
- `bot/jobs.py` — `Job` + `JobManager`: one job per user, auto-cancel of the previous job,
  `Semaphore` queue, the download→mux→thumbnail→upload pipeline, cleanup, counters.
- `bot/download.py`, `bot/upload.py`, `bot/ffmpeg.py`, `bot/probe.py`, `bot/qualities.py`,
  `bot/progress.py`, `bot/process.py` — the engine: TypeTube CLI invocation, delivery to Telegram,
  FFmpeg work, link inspection, quality options, status reporting, subprocess registry.
- `bot/texts.py` — every user-facing string. New text goes here, never inline in handlers.
- `scripts/e2e_local.py` — the end-to-end acceptance test (real bot, mock Bot API, real
  clip). `tests/` — pytest suite.
- `docs/` — `DEPLOYMENT.md` (operator runbook), `TROUBLESHOOTING.md` (symptom → fix),
  `ARCHITECTURE.md` (module map, lifecycles, security model).

## Runtime boundaries (do not break these)

- **Async correctness:** nothing blocking in coroutines. The TypeTube CLI, FFmpeg, ffprobe, DNS and
  disk walks go through `asyncio.to_thread` or a subprocess registered in
  `ProcessRegistry`. A blocking call in a handler freezes the bot for every user.
- **Process ownership:** every child process must be registered in `ProcessRegistry`,
  otherwise `/cancel` silently stops working for that phase.
- **Job contract:** one job per user, one status message per job (owned by `StatusReporter`,
  which is idempotent), one temp directory per job (`DOWNLOAD_DIR/job-<id>-*`), cleanup on
  every exit path — success, failure, cancel and SIGTERM.
- **Delivery:** videos go out with `sendVideo` (streaming, thumbnail, real metadata from
  ffprobe on the local file). A document is only a fallback when Telegram refuses the
  container, and that fallback must be logged.
- **Startup order is an invariant, not a preference:** `bot/runner.py` binds the HTTP port
  *before* any call to Telegram, and the MTProto client is connected in a background task
  (`bot.app.start_uploader`), never awaited in the startup path. A platform fails a deploy
  whose health check does not answer in time, and the handshake can take minutes. `tests/test_startup.py`
  exists to keep it that way; `/health` reports `mtproto` as data and must never fail because
  of it.
- **Uploads are Pyrogram's standard calls:** `send_video` for video (streamable, with real
  dimensions and duration), `send_audio` for audio, `send_document` for everything else, each
  with `progress_for_pyrogram()` so the user sees percentage, bytes, speed and ETA, and so a
  cancel aborts the transfer immediately.
- **Honesty:** never state a capability in the UI, logs or docs that the code does not have.
  The upload ceiling is Telegram's — 50 MB on the cloud Bot API, 2000 MB over MTProto with the
  bot token, 2000 MB on a self-hosted local Bot API server. Nothing in this application may cap
  it, and 4000 MB (Premium user accounts) must never be claimed for a bot.
- **No user session:** large uploads are authorised by `BOT_TOKEN` + `TELEGRAM_API_ID` /
  `TELEGRAM_API_HASH` only. Never introduce a session string, a phone login or an OTP step.
- **Security:** keep the SSRF guard in `bot/urls.py` intact (scheme allow-list, blocked
  hosts, DNS-resolution check); `ALLOW_PRIVATE_URLS=true` is a testing-only escape hatch, so
  do not make it the default or hide it. Secrets are read from the environment and are never
  logged — `Settings.summary()` prints a presence flag instead of a value.

## Before finishing changes

1. `ruff check .` and `ruff format --check .` (ruff also formats the code blocks inside
   markdown docs — run it after editing them).
2. `pytest` — must be green; the FFmpeg tests skip themselves without a real
   ffmpeg+ffprobe pair, so run them with `FFMPEG_BINARY`/`FFPROBE_BINARY` set when the
   change touches media handling.
3. `python main.py --check` — configuration, FFmpeg, disk and versions.
4. `python scripts/e2e_local.py` when the download, mux, thumbnail or upload path changed.
5. Update the docs in the same commit: `README.md` (users), `docs/DEPLOYMENT.md`
   (operators), `docs/ARCHITECTURE.md` (contributors), `CHANGELOG.md` (everyone).
6. Never claim a deployment, CI run or runtime test succeeded unless its output confirms it.
