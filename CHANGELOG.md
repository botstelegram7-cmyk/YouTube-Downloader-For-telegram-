# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
semantic versioning for its configuration surface and behaviour.

## [3.2.0] — 2026-10-12

**A new download engine, and real 2 GB uploads.**

### Added

* **Large uploads through a Telegram user session (MTProto, Pyrofork).** The Bot API stops
  bots at 50 MB; a user session reaches **2000 MB** per file (4000 MB on a Premium account).
  The session uploads the file and the bot then delivers it with a server-side copy, so the
  message is still the bot's own, carries the caption, and nothing is uploaded twice.
  Configure with `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` and `TELEGRAM_SESSION_STRING`;
  `scripts/make_session.py` generates the session string interactively.
* Colour-coded quality buttons (`primary` / `success` / `danger`, Bot API 9.4). Older clients
  ignore the field and keep rendering their usual buttons.
* `/limits` now explains the ceiling of the transport that is actually in use, and how to
  raise it.

### Changed

* **The download engine is now the TypeTube CLI** — no yt-dlp anywhere. Node.js 22.15+ is an
  image dependency (`npm install -g typetube@1.1.4`); the CLI is driven through streamed,
  cancellable processes and its `Completed:`/`[Error]` output is the source of truth, because
  it always exits 0.
* Quality buttons come from the streams TypeTube reports, preferring H.264/MP4 so the result
  plays in Telegram; sizes are only estimated where they can be (audio).
* Links are validated and canonicalised to YouTube before they reach the engine. The engine
  treats unknown URLs as search queries, so `youtu.be`, `/shorts`, `/live`, `/embed`, music
  hosts and bare video ids are all normalised, and everything else is refused with a reason.
* `MAX_UPLOAD_MB` above what the transport can do is **clamped with a warning instead of
  failing validation**. An unattainable ceiling used to abort a deployment; it now never takes
  a working bot offline.
* `MAX_DURATION_MINUTES` is enforced: longer videos are refused before anything downloads.
* `python main.py --check` validates Node, the TypeTube CLI and the transport, and reports
  which upload route is active.

### Removed

* yt-dlp, its configuration surface (`COOKIES_FILE`, `PROXY`, `YTDLP_*`, `QUALITY_LADDER`,
  `AUDIO_FORMAT`, `ALLOW_PLAYLIST`, `PREFER_FREE_FORMATS`, `FRAGMENT_CONCURRENCY`) and the
  SSRF guard that only existed for direct media URLs. `bot/probe.py` is gone.

## [3.0.0] — 2026-10-12

**Complete rewrite in Python.** The serverless handler and the separate download worker are
gone; the bot is now one Python web service (python-telegram-bot 22.8 + yt-dlp + FFmpeg)
that deploys as an ordinary Docker Web Service.

### Added

* **`config.py`** — the whole configuration surface in one place: a frozen `Settings`
  dataclass with sensible defaults for every value, range validation with readable errors,
  derived values (`upload_limit_mb`, `runs_webhook`, `api_mode`) and a startup summary that
  never prints secrets.
* **Quality picking from real data.** Links are probed with yt-dlp, and direct media URLs
  (`.mp4` and friends, where yt-dlp reports no geometry) are inspected with **ffprobe**, so
  the inline keyboard offers only the heights the source actually has — plus `Best`, an
  audio-only option, and `Original` for a raw file. Estimates that come from bitrate rather
  than a reported size are prefixed with `≈`.
* **Playable delivery.** `sendVideo` with `supports_streaming`, real `duration`/`width`/
  `height` taken from the downloaded file, an FFmpeg-generated JPEG thumbnail (≤200 kB,
  ≤320 px), a `+faststart` remux when the container needs it, and a clean HTML caption with
  the source link. A document is only sent when Telegram refuses the video container.
* **Request management.** One job per user: a new link cancels the previous job (processes
  terminated, temp files deleted, old status message rewritten). `/cancel` stops the
  download, conversion and upload in flight and cleans up after itself.
* **Progress and status.** A single status message per job, edited at most once per
  `PROGRESS_INTERVAL` (2 s), carrying a one-tap **❌ Cancel download** button, with separate
  `⬇️`/`🎛`/`⬆️` phases and a byte-accurate upload bar. The message is deleted when the job
  is delivered.
* **Web service.** aiohttp serves `/` (service card), `/health` + `/healthz` (503 when
  FFmpeg is missing), `/stats` (job snapshot) and `POST /<WEBHOOK_PATH>` — secret-token
  checked, 2 MB payload cap, immediate 200, update queued. The bot registers its webhook at
  startup and shuts down gracefully on SIGTERM.
* **Startup introduction.** A console banner with the effective configuration and a
  formatted Telegram message (features, limits, command list) sent to
  `STARTUP_NOTIFY_CHAT_ID`, plus the command menu published via `setMyCommands`.
* **Large files, honestly.** `MAX_UPLOAD_MB=0` now means "as much as this API allows":
  50 MB on Telegram's cloud API, **2000 MB** when `TELEGRAM_API_BASE` points at a
  self-hosted Bot API server. `docker-compose.yml` runs that server (`--local`) next to the
  bot. The real ceiling is printed at startup, in `/limits`, and enforced *before*
  downloading.
* **Tests and CI.** 70 pytest tests (configuration, URL safety, quality selection, jobs,
  progress, real-media FFmpeg), an end-to-end harness (`scripts/e2e_local.py`) that runs the
  real bot against a mock Bot API and a real clip and asserts the whole delivery, and a CI
  workflow with test, Docker-build and hygiene jobs.

### Changed

* **Version 3.0.0**, `python 3.10+` (image: 3.12), dependencies pinned in
  `requirements.txt` (`python-telegram-bot[rate-limiter]==22.8`, `yt-dlp`, `aiohttp`).
* **Commands** are now `/start /help /status /cancel /limits /id /about`. The old
  `/quality`, `/audio`, `/video`, `/download`, `/setup`, `/config`, `/stats` and `/unset`
  are gone: quality is chosen per download from the buttons, and the environment is the
  configuration.
* **Deployment** is a Docker web service: `render.yaml` declares it as a blueprint, the
  `Dockerfile` builds it, and the health check is HTTP `/health`.

### Removed

* The entire serverless side: `tgcloud/` handlers, `tgcloud.jsonc`, the `@tgcloud/cli`
  dependency and `package.json`/`package-lock.json` — no serverless runtime, no CLI token,
  no handler deployment step.
* The Node.js download worker (`worker/`) and its HTTP API, shared secret and Dockerfile —
  the bot now does the downloading itself.
* The TypeTube integration and its fallback path, along with `docs/TYPETUBE_COMPAT.md`.
* The Node test suite, `node --test` CI jobs and `.node-version`.

### Fixed

* **Format selection can no longer fail on thin metadata.** Every generated selector ends in
  a plain `best` fallback, so pressing a quality button on a direct link (where yt-dlp
  reports no heights at all) downloads the file instead of raising "Requested format is not
  available".
* **Upload progress is measured, not guessed.** `UploadProgressFile` counts the bytes the
  HTTP client actually reads from disk (a buffered design previously made the counter jump
  to 100 % on the first read).
* **Status messages cannot be overwritten by a race.** `finish()` is idempotent: the first
  outcome wins, so a cancel arriving after the final edit does not rewrite the result.
* **The non-Unix signal fallback bound its signal name late** (`lambda` capturing the loop
  variable), so both handlers reported `SIGINT`; the name is now bound per iteration.
* **Job cancellation no longer swallows `CancelledError`** through an over-eager
  `except Exception`, and children are always reaped.
* **The startup introduction is actually delivered**: `Application.initialize()` does not
  run `post_init`, so the runner calls `on_startup()` explicitly — verified by the e2e
  harness, which asserts the message arrives.

## [2.0.1] — 2026-10-12

Two things asked for after the first real deployment: no size limit of our own, and a
worker that deploys cleanly on hosts that build the repository root.

### Changed

* **The worker no longer caps upload size.** `MAX_UPLOAD_MB` now defaults to **`0` —
  unlimited**: the file is downloaded at the quality that was requested and handed to
  Telegram, instead of being silently re-downloaded smaller. Set it to a number to bring
  the automatic downgrade back (`AUTO_DOWNGRADE=true`).
* The startup warning, `/health` (`"maxUploadMb": "unlimited"`) and the user-facing
  message now explain **Telegram's own ceiling** instead of ours: 50 MB on the hosted Bot
  API, 2 GB with a self-hosted bot API server via `TELEGRAM_API_BASE`.
* Test and lint globs are shell-expanded (`node --test test/*.test.js`) so they work on
  Node 20 and Node 24 alike.

### Added

* **Root `Dockerfile`** and `.dockerignore` — the same image as `worker/Dockerfile`, built
  from the repository root for hosts that only look for `/Dockerfile`.
* **Root `start` and `build` scripts** (`node worker/src/index.js`, install the worker's
  production dependencies) so a buildpack host can deploy without a root-directory
  setting.
* **`.node-version`** pinning Node 24: buildpack hosts keep new services on Node 20
  otherwise, where TypeTube cannot run.
* **Voroa deployment runbook** (Mumbai, India) in `docs/DEPLOYMENT.md`, with the Docker
  and buildpack paths, the free-plan limits, and the cause of silent (no-FFmpeg) video.
* Troubleshooting entries for the size ceiling and for video arriving without sound.

## [2.0.0] — 2026-10-12

A ground-up rewrite around one architecture: **Telegram Serverless for the bot, a Render
worker for the downloads**, with TypeTube as the primary engine.

### Added

* **Serverless-first bot.** Handlers for `message` and `callback_query`, a SQLite schema
  (`user_preferences`, `bot_settings`, `download_jobs`) and a documented module layout
  enforced by `scripts/check_modules.mjs`.
* **Inline buttons**: quality picker, Cancel, and one-tap "Retry 480p/360p" after a file
  is rejected for exceeding Telegram's 50 MB upload limit.
* **Database-backed configuration.** `/setup <url> <secret>` stores the worker
  credentials in the bot's own database and deletes your message — no secret is ever
  committed, and no `git update-index --skip-worktree` dance is needed. `/config`,
  `/stats` and `/unset` manage it.
* **Automatic quality downgrade.** When a muxed file would exceed `MAX_UPLOAD_MB`, the
  worker retries at the next smaller quality before giving up.
* **Real cancellation.** `AbortController` per job; cancelling kills FFmpeg and yt-dlp,
  removes the temp directory and edits the status message.
* **Live progress** with throttled status edits (download percentage, speed, mux phase,
  upload size) plus periodic chat actions.
* **Better delivery**: thumbnail on `sendVideo`, duration/dimensions metadata, and a
  degradation path `sendVideo → sendVideo (no thumbnail) → sendDocument`.
* **Audio mode** (`/audio`), converting to M4A/AAC only when the source is not already
  AAC, with title/performer metadata.
* **Structured logging** with secret redaction, request and job ids.
* **A real test suite**: 62 worker tests, 69 bot tests that run the actual handlers
  against an in-memory SDK stub, and a shared URL fixture file that keeps the worker's
  and the bot's validators identical.
* **CI** that validates the layout, runs both suites, builds the container and
  smoke-tests `/health` inside it.
* **Documentation**: deployment guide written for Termux, architecture notes,
  troubleshooting table, and the TypeTube/Node compatibility write-up.

### Changed

* The worker is now a set of focused modules (`server`, `queue`, `job`, `downloader`,
  `typetube`, `ytdlp`, `telegram`, `config`, `logger`, `validate`) instead of one file.
* `/download` answers immediately (`202` with a queue position) and the real work happens
  asynchronously, so an update handler never blocks.
* `render.yaml` is now a Blueprint for the **worker** with free-plan-safe defaults and a
  Render-generated shared secret; the region defaults to Singapore to match TypeTube's
  cluster.
* The worker pins **Node 24** and validates its environment at boot, reporting every
  problem at once.

### Fixed

* **`typetube` crashed on import on Node < 22.15** (`promisify(zlib.zstdCompress)` at
  module scope). The worker now checks the runtime, reports `typeTube.supported` in
  `/health`, raises `UNSUPPORTED_RUNTIME` when a job is attempted, and falls back to
  yt-dlp — instead of dying with an `ERR_INVALID_ARG_TYPE` from inside `node:util`.
  A lazily-bound fix is prepared as a pull request against TypeTube.
* **A URL was never checked for a video id**, so channel and playlist links passed
  validation and failed later. Both validators now require a single-video URL.
* **Plain text was treated as an unknown command** ("hello there" → "I do not know
  /hello").
* **`normalizeBaseUrl` mangled explicit non-HTTP schemes**, making `ftp://…` pass worker
  URL validation.
* Duplicate-chat and queue-full errors returned `HTTP 500` instead of `409`/`503`.
* The `/health` response exposed the configuration object and could echo credentials; it
  now reports only versions, capabilities and queue counters.
* Stale job rows blocked new downloads for 30 minutes; the guard is now the cooldown
  window, and the worker's own `409` is the authoritative answer.
* The retry button refused to run because the finished job still counted as active.
* `supportsZstd()` wrongly accepted Node 23.0–23.7, where zlib zstd does not exist.
* CI ran `npm ci` without a lockfile, so every build failed.

### Removed

* The legacy Python polling bot (`bot.py`, `config.py`, `requirements.txt`, the root
  `Dockerfile`, `examples/`). Running a poller and a webhook against the same bot token
  breaks both; Telegram Serverless owns the webhook now.
* The old `scripts/check-tgcloud.mjs`, replaced by `scripts/check_modules.mjs` with
  stricter, testable rules.
