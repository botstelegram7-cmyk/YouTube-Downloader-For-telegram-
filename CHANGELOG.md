# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses semantic
versioning for the worker's HTTP contract.

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
