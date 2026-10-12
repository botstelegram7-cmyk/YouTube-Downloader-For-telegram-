# YouTube Downloader for Telegram

A Telegram bot that turns a video link into a **playable file**: it inspects the link,
offers only the qualities that link really has, downloads the one you pick with
[yt-dlp](https://github.com/yt-dlp/yt-dlp), fixes it up with FFmpeg, and uploads it as a
streaming-ready video with a thumbnail, real metadata and a clean caption.

Everything is Python. There is no serverless component, no Node.js and no external
worker — the bot *is* the web service.

```
link ──▶ inspect ──▶ quality buttons ──▶ download ──▶ mux + faststart ──▶ thumbnail ──▶ sendVideo
```

---

## Contents

- [Highlights](#highlights)
- [Requirements](#requirements)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Commands](#commands)
- [Repository layout](#repository-layout)
- [How a download is processed](#how-a-download-is-processed)
- [Testing](#testing)
- [Limits, honesty and legal notes](#limits-honesty-and-legal-notes)
- [Documentation](#documentation)
- [License](#license)

---

## Highlights

**Quality picking that tells the truth**
Send a link and the bot probes it with yt-dlp (plus FFmpeg for direct media URLs), then
shows inline buttons for the heights the source actually offers — `144p … 2160p`, plus
`Best` and an audio-only option. A 240p clip offers "240p", never a fake "1080p" that
would silently download something else.

**Playable videos, not files**
Videos are delivered with `sendVideo`: `supports_streaming=True`, `duration`, `width`,
`height`, an FFmpeg-generated JPEG thumbnail (≤200 kB, ≤320 px, as Telegram requires) and
a caption with the title, quality, resolution, size and source link. MP4 files are
remuxed with `+faststart` so playback can begin before the download of the file finishes.
A document is only sent when Telegram refuses the container itself — and that is logged.

**One request per user**
A new link from the same user cancels the previous job first: its yt-dlp/FFmpeg processes
are terminated, its temp directory is deleted, and the old status message is rewritten to
"♻️ Cancelled your previous request to start the new one."

**`/cancel` that really cancels**
Cancels the download, the conversion *and* the upload in flight, for every job that user
owns, kills the child processes, and removes the temp files.

**Honest about size**
The bot never claims a limit it cannot deliver. Telegram's cloud Bot API caps uploads at
**50 MB**; a self-hosted Bot API server (`--local`) raises that to **2000 MB**. The bot
detects which API it is talking to, reports the real ceiling in `/limits` and in the
startup banner, and refuses early — before downloading — when the estimate already exceeds
it. The 2 GB path is included as a ready-to-run `docker compose` service; see
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#step-4--uploads-larger-than-50-mb-the-local-bot-api-server).

**Built for a long-running service**
aiohttp serves `/health` (503 if FFmpeg is missing), `/stats` and the webhook endpoint;
the bot registers its webhook at startup, publishes its command menu, posts a startup
introduction, survives SIGTERM with a graceful shutdown, and cleans up after itself.

---

## Requirements

| Need | Version / note |
|---|---|
| Python | 3.10 or newer (Docker image uses 3.12) |
| FFmpeg + ffprobe | any recent build, on `PATH` or set via `FFMPEG_BINARY` / `FFPROBE_BINARY` |
| Telegram bot token | from [@BotFather](https://t.me/BotFather) |
| Disk | temp files land in `DOWNLOAD_DIR` (default `/tmp/ytbot-downloads`) |
| Python packages | `python-telegram-bot[rate-limiter]==22.8`, `yt-dlp`, `aiohttp` |

---

## Quick start

### 1. Talk to BotFather

```
/newbot            → follow the prompts, copy the token
/setcommands        → see docs/BOT_PROFILE.md for a ready-made list
/setdescription     → optional, the same file has copy for it
```

### 2. Run it — Docker (recommended)

```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
cp .env.example .env
$EDITOR .env            # at minimum: BOT_TOKEN=..., and OWNER_USER_ID=your numeric id

docker compose up -d --build
docker compose logs -f bot
```

The included `docker-compose.yml` starts **two** services: the bot and a local Bot API
server, which is what unlocks 2000 MB uploads. If you only need the 50 MB cloud API, run
the bot alone:

```bash
docker build -t ytbot .
docker run -d --name ytbot --env-file .env -p 8080:8080 \
  -v ytbot-downloads:/tmp/ytbot-downloads ytbot
```

### 3. Run it — locally, without Docker

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="123456:ABC..."
export STARTUP_NOTIFY_CHAT_ID=123456789     # optional: get the intro in Telegram

python main.py --check                      # verify config, FFmpeg, disk, versions
python main.py                              # webhook mode when WEBHOOK_URL is set, else polling
```

`main.py` also understands `--online` (call `getMe` to prove the token), `--polling`,
`--webhook`, `--print-config`, `--log-level` and `--version`. Run `python main.py -h`.

### 4. Deploy as a Web Service

Pick whichever fits — each one is a few clicks, and the full runbook for both lives in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md):

| Host | How |
|---|---|
| **Render** | Dashboard → *New → Blueprint* → select this repo. `render.yaml` declares a Docker web service, the `/health` check and every environment variable. |
| **Voroa / any Docker host** | Build the repository root `Dockerfile`, expose `PORT`, set the env vars, keep `HEALTHCHECK` (it is already in the image). |
| **Fly.io / Railway / VPS** | Same idea: the image is self-contained; only `BOT_TOKEN` is mandatory. |

Free tiers sleep after ~15 minutes of inactivity; the bot wakes on the next update, and
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#step-6--keep-a-free-instance-awake) shows the
uptime-monitor trick that keeps webhook mode instant.

---

## Configuration

Every setting has a sensible default, is documented in `.env.example`, and is read once at
startup by `config.py` (a frozen `Settings` dataclass — no hidden global state). Only
`BOT_TOKEN` is mandatory.

### Telegram and identity

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | **Required.** Token from @BotFather. |
| `OWNER_USER_ID` | `0` | Your numeric Telegram id. Only this user may run the bot when set; `/id` tells you your id. |
| `STARTUP_NOTIFY_CHAT_ID` | `0` | Chat that receives the startup introduction + command list. |
| `TELEGRAM_API_BASE` | *(cloud)* | Base URL of a self-hosted Bot API server, e.g. `http://telegram-bot-api:8081`. Also switches the upload ceiling to 2000 MB. |
| `MAX_UPLOAD_MB` | `0` | `0` = auto (50 MB cloud, 2000 MB local API). A number pins the ceiling lower. |
| `MAX_DURATION_MINUTES` | `0` | `0` = no limit. Otherwise refuse links longer than this. |
| `DOWNLOAD_DIR` | `/tmp/ytbot-downloads` | Where jobs work. Each job gets its own `job-<id>-*` subdirectory, removed when it ends. |

### Quality and formats

| Variable | Default | Meaning |
|---|---|---|
| `DEFAULT_QUALITY` | `720p` | Height pre-selected in the keyboard when available. |
| `AUDIO_FORMAT` | `m4a` | `m4a` (no re-encode) or `mp3` (transcoded by FFmpeg). |
| `OFFER_AUDIO_ONLY` | `true` | Show the "Audio · M4A" button. |
| `ALLOW_PLAYLIST` | `false` | Accept playlist links (first entry only). Off by default to avoid surprise downloads. |

### Downloading

| Variable | Default | Meaning |
|---|---|---|
| `COOKIES_FILE` | — | Netscape-format cookies file, for age-restricted or sign-in-only videos. |
| `PROXY` | — | Proxy URL handed to yt-dlp (`http://`, `socks5://`). |
| `YTDLP_SOCKET_TIMEOUT` | `30` | Per-socket timeout, seconds. |
| `YTDLP_RETRIES` | `10` | yt-dlp retries per fragment. |
| `FRAGMENT_CONCURRENCY` | `4` | Parallel fragments for DASH/HLS sources. |
| `DOWNLOAD_TIMEOUT_SECONDS` | `3600` | Hard ceiling for one download; the job fails cleanly past it. |
| `PREFER_FREE_FORMATS` | `true` | Prefer formats that do not need a signature workaround. |
| `USER_AGENT` | *(yt-dlp default)* | Override the HTTP user agent. |
| `FFMPEG_BINARY` / `FFPROBE_BINARY` | `ffmpeg` / `ffprobe` | Explicit tool paths (useful on odd hosts). |
| `ALLOW_PRIVATE_URLS` | `false` | **Testing only.** Permits loopback/private links; it disables the SSRF guard. |

### Uploads, progress and housekeeping

| Variable | Default | Meaning |
|---|---|---|
| `PROGRESS_INTERVAL` | `2.0` | Minimum seconds between two status-message edits. |
| `UPLOAD_CHUNK_SIZE` | `1048576` | Read size used to report upload progress. |
| `THUMBNAIL_MAX_SIDE` | `320` | Longest side of the generated thumbnail (Telegram caps at 320). |
| `MAX_CONCURRENT_JOBS` | `2` | Jobs allowed to run at once; extra jobs wait in a queue. |
| `CLEANUP_AFTER_UPLOAD` | `true` | Delete the job directory once the file is delivered. |
| `KEEP_FAILED_FILES` | `false` | Keep files of failed jobs for inspection. |

### Web service and logging

| Variable | Default | Meaning |
|---|---|---|
| `WEBHOOK_URL` | — | Public HTTPS base URL, e.g. `https://mybot.onrender.com`. Set → webhook mode. Same as `PUBLIC_URL`; `RENDER_EXTERNAL_URL` is picked up automatically. |
| `WEBHOOK_PATH` | `telegram` | Path the webhook is served on: `POST /telegram`. |
| `WEBHOOK_SECRET` | — | Secret token Telegram echoes back in `X-Telegram-Bot-Api-Secret-Token`; requests without it are rejected with 403. Generated for you by `render.yaml`. |
| `LISTEN_HOST` | `0.0.0.0` | Interface for the web server. |
| `PORT` | `8080` | Port for the web server (read from `PORT` automatically). |
| `USE_POLLING` | `false` | Force long polling even when `WEBHOOK_URL` is set. |
| `LOG_LEVEL` | `INFO` | `DEBUG`, `INFO`, `WARNING`, `ERROR`. |
| `LOG_PRETTY` | `true` | Human-readable console logs; `false` emits JSON-ish key=value lines. |

---

## Commands

| Command | What it does |
|---|---|
| `/start` | Welcome screen with the feature list. |
| `/help` | Step-by-step usage, limits and tips. |
| `/status` | Active jobs, queue, free disk, versions, effective limits. |
| `/cancel` | Stops your download, conversion and upload, and cleans its temp files. |
| `/limits` | Why files are capped, the real ceiling for the current API, and how to raise it. |
| `/id` | Your numeric id — put it in `OWNER_USER_ID` / `STARTUP_NOTIFY_CHAT_ID`. |
| `/about` | Version, engine versions, credits. |

The same list is published to Telegram at startup, so it appears in the bot's menu button.

**Inline buttons**

| Button | Callback data | Meaning |
|---|---|---|
| `144p … 2160p`, `Best`, `Original`, `Audio · M4A` | `dl:<quality>:<token>` | Download at that quality. The token is bound to the user and the probed link, and is consumed on first use. |
| `❌ Cancel download` | `cancel:<job id>` | Stops the running job. |
| `↻ Try a smaller quality` | `abort:<token>` | Offered after a failure, with the smaller qualities the source really has. |

---

## Repository layout

```
main.py                   # entry point + CLI: --check, --online, --polling, --webhook, --print-config
config.py                 # every setting, its default, validation and the startup summary
bot/
  app.py                  # builds the PTB Application: handlers, timeouts, startup introduction
  runner.py               # lifecycle: initialize → start → web server + webhook/polling → SIGTERM → shutdown
  web.py                  # aiohttp app: /, /health, /healthz, /stats, POST /<WEBHOOK_PATH>
  handlers/               # commands.py, links.py, callbacks.py, errors.py (thin: they delegate)
  jobs.py                 # Job + JobManager: one job per user, auto-cancel, queue, progress, cleanup
  download.py             # yt-dlp wrapper: options, progress hooks, cancellation, result metadata
  upload.py               # sendVideo/sendAudio/sendDocument with thumbnail, caption and progress
  ffmpeg.py               # probe, faststart remux, thumbnail, audio conversion, version
  probe.py                # link inspection + short-lived ProbeCache (tokens for the buttons)
  qualities.py            # quality options built from the real formats, size estimates, selectors
  progress.py             # throttled status edits (idempotent) and the upload progress stream
  process.py              # ProcessRegistry: spawn, stream, terminate — used for every child process
  keyboards.py, texts.py, utils.py, urls.py, errors.py, logging_setup.py, runtime.py
scripts/e2e_local.py      # end-to-end harness: real bot + mock Bot API + real media (see Testing)
tests/                    # pytest suite (70 tests)
docs/                     # DEPLOYMENT, TROUBLESHOOTING, ARCHITECTURE
Dockerfile, docker-compose.yml, render.yaml, .env.example, requirements*.txt
```

---

## How a download is processed

1. **Link in.** The handler extracts the URL, checks it against the SSRF guard (no
   loopback, private or link-local hosts), and answers within milliseconds with
   "🔍 Analysing…".
2. **Probe.** yt-dlp lists formats *without downloading*; direct media URLs are inspected
   with ffprobe so their real width/height/duration/size are known. A short token is
   cached for that user.
3. **Quality.** Buttons appear for the heights the source supports, each with a size
   estimate and a `≈` when the estimate is derived from bitrate rather than a real
   `filesize`.
4. **Download.** `JobManager` cancels the user's previous job, registers the new process
   in `ProcessRegistry` (so `/cancel` can kill it), starts yt-dlp in a worker thread and
   streams `⬇️` progress from yt-dlp's hooks into one edited status message.
5. **Finish.** FFmpeg remuxes to MP4 with `+faststart` when needed, generates a JPEG
   thumbnail, and ffprobe reads the geometry of the *local file* — so the metadata that
   goes to Telegram is real even when the source reported nothing.
6. **Upload.** `sendVideo` with `supports_streaming`, duration/dimensions, thumbnail and
   caption; progress is reported from the bytes actually read from disk. The status
   message is deleted, and the job directory is removed unless it is still needed.

Details, failure modes and the security model: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

---

## Testing

```bash
pip install -r requirements-dev.txt

pytest                          # 70 unit + integration tests
ruff check . && ruff format --check .
python main.py --check          # config, FFmpeg, disk, versions

# end-to-end, no Telegram account needed:
# builds a real 3-second H.264/AAC clip, serves it over HTTP, runs the real bot
# against a mock Bot API, picks a quality, then asserts the whole delivery
python scripts/e2e_local.py
```

`scripts/e2e_local.py` is the acceptance test for the pipeline. It verifies the startup
introduction, the command menu, the quality buttons, `sendVideo` with caption + thumbnail
+ `supports_streaming` + dimensions + duration, that the status message is cleaned up,
that temp directories are removed, and that SIGTERM shuts the bot down without a
traceback. It needs `ffmpeg`/`ffprobe` on `PATH` (or `--ffmpeg <dir>`).

FFmpeg-dependent tests skip themselves when no FFmpeg is installed, so `pytest` is safe
on a bare machine.

CI (`.github/workflows/ci.yml`) runs three jobs on every push: **test** (ruff, import
check, pytest, `main.py --check`, the e2e harness), **docker** (build the image, run
`--check` and the e2e *inside* the image) and **hygiene** (no secrets, no serverless
leftovers, no stray artifacts).

---

## Limits, honesty and legal notes

* **Upload size.** Telegram itself is the limit: **50 MB** on the cloud Bot API and
  **2000 MB** when the bot talks to a self-hosted Bot API server. The bot prints the real
  ceiling at startup, shows it in `/limits`, and refuses oversized files before
  downloading instead of failing at the end. It cannot exceed what the API in front of it
  allows, whatever `MAX_UPLOAD_MB` says.
* **This is a general yt-dlp front end.** It works with any site yt-dlp supports. It ships
  no keys, no bypasses and no DRM handling: DRM-protected, paywalled, private or
  sign-in-only content needs your own `COOKIES_FILE`, and content you are not allowed to
  download is out of scope.
* **Respect the law and the terms of the sites you use.** Downloading copyrighted material
  without permission may be illegal where you live. You are responsible for what you
  download with your own bot token.

---

## Documentation

| Document | Contents |
|---|---|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | BotFather setup, Render blueprint, any Docker host, the local Bot API server for 2000 MB uploads, keep-awake, day-two operations. |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | Every symptom we have seen: container exits, 503 health, webhook silence, missing FFmpeg, 403s, oversized files, stalled uploads. |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | Module map, one download's lifecycle, job/state model, security model, testing strategy. |
| [CHANGELOG.md](CHANGELOG.md) | Version history, including the 3.0.0 Python rewrite. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Dev setup, style, how to add a feature without breaking the pipeline. |
| [SECURITY.md](SECURITY.md) | How to report a vulnerability, what the bot stores, hardening checklist. |
| [BOT_PROFILE.md](BOT_PROFILE.md) | Copy-paste text for BotFather: description, about, command list. |

---

## License

[MIT](LICENSE).
