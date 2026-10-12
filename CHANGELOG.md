# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project follows
semantic versioning for its configuration surface and behaviour.

## [3.3.3] — 2026-10-12

**Torrents, magnets and direct links; a status card that reports time and speed; and a
`/start` that says only what a stranger needs to know.**

### Added

* **Torrents and magnet links.** Send a `magnet:` link or a `.torrent` URL: the bot fetches
  the *metadata only*, lists the files it really contains with their real sizes (largest
  video first, samples and `.nfo` scenery last) and downloads just the one you tap —
  `aria2c --select-file`, then `--seed-time=0`, so nothing is seeded afterwards. A magnet
  with no peers answers with a plain explanation instead of hanging.
* **Direct file links.** Any `http(s)` URL: the server is asked what it is (name, size,
  type) with a real `Range` request, you confirm, and aria2 fetches it with 16 parallel
  connections. Video and audio arrive playable; anything else arrives as a document.
* **Live speed and ETA on every job.** `📦 412 MB / 1.2 GB · ⚡ 8.4 MB/s · ⏳ ETA 2:18`,
  read from the engine's own output and from the bytes really on disk. The ETA only appears
  when a total size is known, because otherwise it would be a guess.
* **`/diag` reports the fetcher**, and `main.py --check` names it (a missing `aria2c` is a
  warning, not a failure: YouTube downloads do not need it).

### Changed

* **`/start` is short and public.** It shows the picture, what the bot accepts (YouTube ·
  torrent · direct) and two shortcuts — no engine name, no size ceilings, no command dump,
  and no owner id (`SHOW_OWNER_IN_START=false` by default now). The command cheat sheet
  lives in `/help`, `/limits` keeps the ceilings, and `/diag` is not advertised to anyone but
  the owner (it is published in the owner's private command scope only).
* **Status edits are spaced by at least `PROGRESS_MIN_INTERVAL` (5 s).** Telegram allows
  about one message per second per chat; the floor keeps a busy bot well clear of the limit.
  The first update that carries real numbers is always shown immediately, so a short job
  still shows its speed and ETA instead of "Starting…" until it finishes.
* **What a file *is* is decided by ffprobe, not by its name.** Random bytes named `.mp4`
  make ffprobe report a `bintext` 1280×3200 "video": that guess is no longer trusted, and a
  file with no readable media is delivered as a document without invented dimensions,
  duration or thumbnail. Real MP4s are still recognised through ffprobe's container list
  (`mov,mp4,m4a,3gp,3g2,mj2`), and audio-only containers are not mistaken for video.
* Every expected job failure is now named in the log (`Job <id> failed (<type>): <reason>`),
  and a link that cannot be read is logged with the reason instead of only being refused in
  chat.

### Fixed

* **A link can no longer point inside the server's own network.** Loopback, private,
  link-local, reserved and multicast addresses (and `localhost`, `*.internal`,
  `metadata.google.internal`) are refused before anything is fetched. `ALLOW_PRIVATE_LINKS=true`
  turns that off for local testing.
* IPv6 URLs are no longer truncated at the closing bracket, so `http://[::1]/x` is refused as
  a private address instead of being misread as `http://[::1`.

### Security note

A bot that will fetch any URL a stranger sends is a request-forgery tool aimed at whatever
network it runs in. That is why the address guard above is on by default, and why it is
documented in `.env.example` rather than left implicit.

## [3.3.2] — 2026-10-12

**"Bot went offline after /start", the slow download path, and a bot anyone can use.**

The two complaints behind this release were "download speed so slow, fix those" and "after
/start the bot got offline". Both turned out to have a cause that could be measured, and
both are now pinned by tests.

### Fixed

* **A refused webhook no longer looks like a registered one.** `setWebhook` failing was
  logged as `webhook registered`, so the bot reported perfect health while Telegram delivered
  *zero* updates — which is exactly what "offline after /start" looks like. Registration now
  returns a verdict, the phase line says `webhook REFUSED by Telegram`, and the bot **falls
  back to long polling** so it stays usable while the operator fixes the URL.
* **A stale `WEBHOOK_URL` can no longer hide the real address.** The service was renamed on
  Render and the old host was kept in `WEBHOOK_URL`: Telegram then delivered updates to a
  host that no longer answers. When `RENDER_EXTERNAL_URL` is present and disagrees, the
  platform's own address wins, a startup warning names both URLs, and `/diag` shows where the
  URL came from and flags the ignored one.
* **The webhook secret is cleaned instead of refused.** Telegram accepts only
  `A-Z a-z 0-9 _ -` in `secret_token`; a base64 value (`Ab+C/D=E==`, what `generateValue`
  produces) made every registration fail. It is now sanitised to `AbCDE` with a warning
  naming the new value.
* **The `/start` caption no longer exceeds Telegram's 1024-character limit.** The greeting
  rides on the picture, the command list follows as its own message, and a text fallback
  splits at 4096 instead of hitting the API limit.

### Added

* **`/start` answers with a real picture** (`assets/start.jpg`) carrying the introduction, the
  add-to-group link and the owner credit, with the picture's `file_id` cached so it is
  uploaded once.
* **An animated progress card** (`assets/progress.gif`, `assets/upload.gif`): the status
  message shows a looping indicator, switches animation between the download and upload
  phases, and edits its caption with live speed and ETA. `RICH_MESSAGES=false` keeps plain
  text; any client or cache that refuses the media edit falls back automatically.
* **A CPU-aware worker count.** `TYPETUBE_WORKERS=0` (new default) reads the container's own
  CPU quota — cgroup v2 `cpu.max`, cgroup v1 `cpu.cfs_quota_us`, else `os.cpu_count()` — and
  asks for 16 workers per core, clamped to 8–32. `MT_CONNECTIONS` goes from 4 to 8.
* **Owner-only diagnostics.** `/diag` (and its refresh button) answers the configured owner
  and nobody else — `OWNER_USERNAME` resolves via `get_chat` at startup, so no numeric id is
  needed. An unconfigured owner grants *nobody* access rather than everyone.
* **A fair-use cooldown** of 5 s per user (`USER_COOLDOWN_SECONDS`, `0` disables), with the
  owner exempt.
* **`/health` for platforms, `/health` for operators:** the public probe returns
  `{ok, service, version, uptime_seconds}` and always answers 200 — it can no longer fail
  because an optional binary or a warming transport is missing. The operator view (the same
  route plus the `X-Telegram-Bot-Api-Secret-Token` header) adds transport state, job counters,
  CPU budget and disk.
* `/stats` gained `application_running` and `queued_updates` — the two numbers that answer
  "is the bot actually listening?".

### Changed

* The bot is **public by default**: no owner is needed to download, and the startup warning
  says so instead of implying a lock.
* `bot/runner.py` reports its startup phases truthfully, and Pyrogram's INFO chatter is
  silenced with the other noisy libraries.

## [3.3.1] — 2026-10-12

**A deploy that failed its health check, fixed — and the Pyrogram upload path made explicit.**

### Fixed

* **The service now binds its port before it contacts Telegram at all.** The MTProto client
  used to be awaited during startup: the handshake (DC discovery, auth-key exchange, DC
  migration) took over a minute on a real deploy, the platform's health check timed out
  meanwhile, and the deployment was reported as failed while the bot itself was healthy.
  Order is now: bind the port → initialise → start → webhook/polling → announce → *then*
  connect MTProto in the background. A regression test reproduces both halves of that
  incident (a stalled connection cannot delay readiness; a failing one is retried and never
  fatal), including an end-to-end scenario, `scripts/e2e_local.py --mtproto-unreachable`,
  that runs in CI with credentials that cannot connect.
* **`/health` reports the transport state** (`mtproto: connecting | ready | error | off`)
  as information, and never fails because of it.
* **The banner shows the effective webhook address** instead of `None` (it printed the raw
  `WEBHOOK_URL`, which is empty when the platform supplies `RENDER_EXTERNAL_URL`).
* `ffmpeg -version` is cached instead of being spawned on every health poll — free-plan
  platforms poll every few seconds.

### Added

* **`MtprotoUploader.progress_for_pyrogram()`** — the callback Pyrogram calls while
  uploading, documented as such: it reports percentage, transferred bytes, speed and ETA to
  the existing status message, and raises from inside the callback when the job is
  cancelled, so the upload stops immediately rather than after the file has been sent.
  Videos go out with `send_video` (streamable, with real dimensions and duration), audio
  with `send_audio`, everything else with `send_document` — each pinned by a test.
* **A "Connecting to Telegram" status** while a large file waits for the MTProto client to
  come up, so a cold connection never looks like a hang.
* `MtprotoUploader.state` / `last_error`, surfaced by `/health`, `/limits` and the logs, so a
  transport that is configured but not connected is never advertised as usable.
* Startup phase timings in the log (`startup[1.84s] HTTP server listening …`), so a slow
  boot is visible in a deploy log instead of inferred.

### Changed

* `/limits` adds a line when the MTProto client is still connecting, or has failed — the
  ceiling it reports is the one that is reachable, not the one the configuration implies.
* Attempts to connect MTProto are retried (3 attempts, 20 s apart) and logged with the
  attempt number; giving up leaves the bot fully functional on the Bot API, and a later
  large-file request retries on demand.

## [3.3.0] — 2026-10-12

**No size limits, and no user session: 2000 MB uploads with the bot you already have.**

### Added

* **Large uploads over MTProto, authorised by the bot token.** `TELEGRAM_API_ID` and
  `TELEGRAM_API_HASH` identify your Telegram *application*; `BOT_TOKEN` still authorises the
  bot. With both set, files that the Bot API cannot carry go out over Telegram's own protocol
  at up to **2000 MB per file** — no second account, no phone number, no login code, and no
  session string to store or leak. The Pyrogram client runs in memory (`in_memory=True`), so
  no `.session` file is ever written.
* **Direct delivery.** The MTProto client *is* the bot, so it sends the file straight into the
  user's chat. The old upload-then-`copy_message` relay is gone: no second pass over the
  network, and nothing is parked in a storage chat.
* **Honest capability reporting.** `/limits`, `/status`, the banner, `/health`, `/stats` and
  `python main.py --check` all state the ceiling the active transport really has, and the
  Bot API route tells the operator exactly which two variables raise it.
* **Disk-space guard.** `MIN_FREE_DISK_MB` (default 256) is a *hosting* constraint, checked
  before a job starts and mapped from engine errors (`ENOSPC`) as `DiskSpaceError`, so "disk
  full" is reported as such instead of as a download failure. Partial files are still deleted
  on every path.
* **Audio-less videos.** When the resolved track has no audio stream, the engine is invoked
  with `--video-only` (its mux mode fails on such videos by design).
* `bot/mtproto.py` is now the bot-token uploader; `bot/upload.py` routes per file and falls
  back to the Bot API when MTProto is unavailable and the file still fits.

### Changed

* **`MAX_UPLOAD_MB` is removed.** Nothing in this application caps a download or an upload —
  neither in the UI, nor in `config.py`, nor in validation. The only ceilings that exist are
  Telegram's protocol limits (50 MB Bot API, 2000 MB MTProto/local Bot API) and the host's
  disk, and they are kept separate everywhere they appear.
* **The oversize check moved to where it belongs**: once, at the end of the pipeline, against
  what the chosen transport can actually send. A file is never refused for a limit this bot
  invented.
* `UPLOAD_TRANSPORT` is now `auto` / `bot` / `mtproto`, and a forced-but-unconfigured
  `mtproto` degrades to the Bot API with a warning instead of failing the deployment.
* The startup banner reports `Upload : up to N MB per file (transport)` instead of a policy
  number.
* `docker-compose.yml` and `render.yaml` no longer set a size variable
  (`MIN_FREE_DISK_MB`, `TELEGRAM_API_ID`, `TELEGRAM_API_HASH` take its place).

### Removed

* `scripts/make_session.py`, `TELEGRAM_SESSION_STRING`, `STORAGE_CHAT_ID`,
  `MT_PREMIUM_ACCOUNT`, `MAX_UPLOAD_MB`, and the user-session upload path with its
  `copy_message` relay. There is no login-backed code path left in the project.
* `bot/texts.py`'s session-instructions message and the `mt_premium` capability branches.

### Fixed

* `python main.py --check` no longer reports the TypeTube CLI as `MISSING` when it is installed
  but silent: version 1.1.4 has no `--version` flag and prints nothing.

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
