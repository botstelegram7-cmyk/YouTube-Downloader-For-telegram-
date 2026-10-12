# YouTube Downloader for Telegram

A production-grade Telegram bot that turns a YouTube link into a **playable video** — or an
audio track — delivered straight into the chat.

* **Engine:** the [TypeTube](https://github.com/botstelegram7-cmyk/typetube) CLI resolves the
  real streams and downloads them with parallel range workers. No yt-dlp.
* **Delivery:** FFmpeg muxes and verifies the result, so the file always plays: real
  dimensions, a duration, a generated thumbnail, `supports_streaming` and a clean caption.
* **Large files:** up to **2000 MB** per file through a Telegram user session (MTProto), or
  4000 MB if that account has Premium. No second service to host.
* **Buttons that say something:** quality choices come from the video's real streams, are
  colour-coded (green = best, blue = alternatives, red = cancel) and are never invented.

---

## Contents

1. [What it does](#what-it-does)
2. [Quick start](#quick-start)
3. [Large files, honestly](#large-files-honestly)
4. [Commands](#commands)
5. [Configuration](#configuration)
6. [How it works](#how-it-works)
7. [Requirements](#requirements)
8. [Development](#development)
9. [Limits and caveats](#limits-and-caveats)
10. [Documentation](#documentation)

---

## What it does

Send a link, get a file:

1. **Resolve** — TypeTube reports the actual streams (`144p … 2160p`, H.264/VP9/AV1, audio
   bitrates) plus title, channel, duration and artwork.
2. **Choose** — the bot offers only what the video really has, recommending one quality in
   green. Nothing is estimated that cannot be known.
3. **Download** — parallel range workers, live progress, one tap to cancel at any moment.
4. **Deliver** — verified with ffprobe, remuxed with faststart so Telegram streams it while
   it downloads, given a JPEG thumbnail and a caption with title, quality, duration, size and
   a link back to the source.

A new link from the same user replaces the running job: the previous download is killed, its
files are deleted, and the user is told. `/cancel` does the same on demand.

## Quick start

```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
cp .env.example .env          # set BOT_TOKEN at minimum
docker compose up -d --build
```

On a platform that builds a Dockerfile (Render, Voroa, Fly.io, any VPS), point it at this
repository and set the same environment variables. The bot serves `/health` (status, FFmpeg,
upload ceiling) and `/stats` on `$PORT`, and runs in webhook mode when `WEBHOOK_URL` is set,
otherwise in long polling.

Minimum viable environment:

| Variable | Why |
|---|---|
| `BOT_TOKEN` | from [@BotFather](https://t.me/BotFather) — the only required setting |
| `WEBHOOK_URL` | your service's public https URL; omit to use long polling |
| `OWNER_USER_ID` | keeps the bot to yourself (send `/id` to find it) |

## Large files, honestly

The number that matters is what Telegram will accept — and it depends entirely on *who* is
uploading:

| Route | Ceiling per file | Needs |
|---|---|---|
| Cloud Bot API (default) | **50 MB** | nothing |
| **User session (MTProto)** — recommended | **2000 MB**, 4000 MB with Premium | `api_id`, `api_hash`, a session string |
| Self-hosted Bot API server (`--local`) | 2000 MB | a second service + ~2× disk |

The recommended route needs no extra service:

```bash
python scripts/make_session.py        # once, on your own machine: asks for a login code
```

Then set the three values it prints:

```dotenv
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
TELEGRAM_SESSION_STRING=BQ...
```

From then on the bot uploads big files **through that account** and delivers them to the user
with a server-side copy, so the message still comes from the bot, carries the caption, and no
bytes travel twice. Files under the Bot API limit keep going through the normal API.

You can set `MAX_UPLOAD_MB=1500` once a session exists; a value above what the transport can
actually do is **clamped with a warning** rather than treated as an error — a misconfigured
ceiling must never take a working bot offline.

## Commands

| Command | What it does |
|---|---|
| `/start` | welcome screen with shortcuts |
| `/help` | the three-step how-to |
| `/status` | running jobs, engine, transport, free disk |
| `/cancel` | stop the download, the muxing and the upload |
| `/limits` | the real ceiling, and how to raise it |
| `/id` | your chat id, for `OWNER_USER_ID` |
| `/about` | version and engine |

## Configuration

Everything has a default; `.env.example` documents all of it. The ones people actually change:

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | — | **required** |
| `OWNER_USER_ID` | `0` | `0` leaves the bot open to anyone |
| `WEBHOOK_URL` | *(empty)* | empty → long polling |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | [my.telegram.org](https://my.telegram.org); used for big uploads |
| `TELEGRAM_SESSION_STRING` | — | from `scripts/make_session.py`; treat it like a password |
| `MT_PREMIUM_ACCOUNT` | `0` | `1` only if that account really has Premium (4000 MB) |
| `UPLOAD_TRANSPORT` | `auto` | `auto`, `bot` (never use the session) or `mtproto` |
| `MAX_UPLOAD_MB` | `0` | `0` = the transport's own ceiling; higher values are clamped |
| `MAX_DURATION_MINUTES` | `0` | `0` = unlimited; refuses longer videos before downloading |
| `DEFAULT_QUALITY` | `720p` | the quality highlighted in green; `best` for the maximum |
| `TYPETUBE_API_KEY` | *(empty)* | lifts the cluster's quota and unlocks Hi-Fi audio |
| `TYPETUBE_WORKERS` / `TYPETUBE_CHUNK_MB` | `8` / `10` | parallel range downloads |
| `MAX_CONCURRENT_JOBS` | `2` | 1–2 on a 512 MB instance |
| `DOWNLOAD_DIR` | `/tmp/ytbot-downloads` | temporary workspace, emptied per job |
| `THUMBNAIL_MAX_SIDE` | `320` | Telegram's own limit |
| `PROGRESS_INTERVAL` | `2.0` | seconds between status edits |
| `LOG_LEVEL` | `INFO` | `DEBUG` for diagnosis |

## How it works

```
Telegram update ─▶ bot/handlers/links.py     validate + canonicalise the YouTube link
                 ─▶ bot/typetube.py          resolve  ──▶ TypeTube CLI (--json)
                 ─▶ bot/keyboards.py         colour-coded quality buttons
   button press  ─▶ bot/jobs.py               one job per user, cancellable, temp dirs
                 ─▶ bot/download.py           download ──▶ TypeTube CLI (streamed progress)
                                              mux/faststart/thumbnail ──▶ bot/ffmpeg.py
                 ─▶ bot/upload.py             Bot API  (small files)
                                              MTProto    (big files: session upload + copy)
```

| Module | Responsibility |
|---|---|
| `config.py` | every setting, its default, and the clamping rules |
| `bot/typetube.py` | the engine: resolve, download, progress parsing, track cache |
| `bot/download.py` | download → verify → faststart → thumbnail → size guard |
| `bot/upload.py` | delivery, choosing the transport per file |
| `bot/mtproto.py` | the Pyrofork user session that carries large uploads |
| `bot/jobs.py` | job lifecycle: one per user, cancel, cleanup, progress |
| `bot/keyboards.py`, `bot/texts.py` | buttons and every user-facing string |
| `bot/process.py` | process runners with timeouts, cancellation and streaming output |

## Requirements

* **Python 3.12+** and `python-telegram-bot 22.8`, `pyrofork`, `aiohttp`
* **FFmpeg + ffprobe** on `PATH` (muxing, verification, thumbnails)
* **Node.js 22.15+** and the `typetube` CLI — the Dockerfile installs both
  (`npm install -g typetube@1.1.4`). The CLI muxes by spawning an executable literally named
  `ffmpeg`, so the configured FFmpeg must carry that name.

## Development

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt ruff
pytest -q                       # unit tests (FFmpeg-dependent ones skip when it is absent)
ruff check . && ruff format --check .
python main.py --check          # config + toolchain pre-flight, no network
python scripts/e2e_local.py     # whole pipeline against a mock Bot API and a stubbed engine
```

## Limits and caveats

* **50 MB without a session**, 2000 MB with one (4000 MB on a Premium account). The bot never
  states a ceiling it cannot honour — `/limits` computes it from the active transport.
* **YouTube only.** The engine treats anything else as a search query, so non-YouTube links
  are refused up front with an explanation rather than silently resolved to the wrong video.
* **Whatever the cluster can reach.** Region-locked, private or removed videos fail with a
  plain explanation; uploads are subject to Telegram's own rate limits.
* **Disk and RAM.** Each job needs room for the file plus its remux; a 2 GB delivery on a
  512 MB free tier will not end well.
* **Respect copyright.** Download what you own or are allowed to save.

## Documentation

| Document | Contents |
|---|---|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | BotFather setup, Docker hosts, the user session for big uploads, day-two operations |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | module-by-module walkthrough and design decisions |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | what each error means and how to fix it |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to run the checks before pushing |
| [CHANGELOG.md](CHANGELOG.md) | what changed, and why |
