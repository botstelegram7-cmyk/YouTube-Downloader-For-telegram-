# YouTube Downloader for Telegram

A production-grade Telegram bot that turns a YouTube link into a **playable video** — or an
audio track — delivered straight into the chat.

* **Three kinds of link:** YouTube (real quality picker), **torrents and magnets** (pick the
  exact file inside), and **direct file URLs**. Anything else is refused with a reason.
* **Engine:** the [TypeTube](https://github.com/botstelegram7-cmyk/typetube) CLI resolves the
  real streams and downloads them with parallel range workers. No yt-dlp.
* **Fetcher:** aria2 for magnets, `.torrent` files and direct links — DHT, web seeds,
  `--select-file`, 16 parallel connections per HTTP file. Nothing is ever seeded afterwards.
* **Delivery:** FFmpeg muxes and verifies the result, so the file always plays: real
  dimensions, a duration, a generated thumbnail, `supports_streaming` and a clean caption.
* **Large files:** up to **2000 MB** per file over MTProto, Telegram's own protocol —
  authorised by your bot token plus `api_id`/`api_hash`. No second account, no phone number,
  no login code, no session string.
* **Buttons that say something:** quality choices come from the video's real streams, are
  colour-coded (green = best, blue = alternatives, red = cancel) and are never invented.
* **Fast by default:** parallel range workers sized from the container's own CPU budget
  (`TYPETUBE_WORKERS=0` = automatic), a warmed MTProto uploader and one status message per
  job instead of a stream of them.
* **Made to be looked at:** `/start` answers with a real picture, and downloads run inside an
  animated status card with live speed and ETA.
* **Open to everyone:** anyone can download. The only restricted view is `/diag`, the
  operator's diagnostics panel, which answers the owner and nobody else.

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

0. **Classify** — a YouTube link, a `magnet:`/`.torrent`, or a plain file URL. Loopback,
   private and link-local addresses are refused (unless `ALLOW_PRIVATE_LINKS=true`), because
   a bot that fetches any URL a stranger sends is a request-forgery tool.
1. **Resolve** — for YouTube, TypeTube reports the actual streams (`144p … 2160p`,
   H.264/VP9/AV1, audio bitrates) plus title, channel, duration and artwork. For a torrent,
   only the *metadata* is fetched first and the file list is offered — a season pack is
   30 GB, the episode you want is 400 MB.
2. **Choose** — the bot offers only what the video really has, recommending one quality in
   green. Nothing is estimated that cannot be known.
3. **Download** — parallel range workers, live progress, one tap to cancel at any moment.
4. **Deliver** — verified with ffprobe, remuxed with faststart so Telegram streams it while
   it downloads, given a JPEG thumbnail and a caption with title, quality, duration, size and
   a link back to the source. A file that is not media at all is delivered as a document,
   exactly as the server sent it — and a file whose name lies (a `.bin` full of random bytes
   that ffprobe "reads" as video) is judged by what ffprobe really found, not by its name.
5. **Watch it run** — the status card shows a bar, the bytes done, the **speed** and an
   **ETA** whenever the total size is known, and it is edited no more than once every
   `PROGRESS_MIN_INTERVAL` seconds (5 by default) to stay clear of Telegram's rate limits.

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
| `OWNER_USERNAME` | *optional* — the operator's `@username`; owner-only `/diag`, no fair-use cooldown |
| `OWNER_USER_ID` | *optional* — the same owner as a numeric id (send `/id` to find it) |

## Large files, honestly

Two things decide how big a file can be, and neither of them is this application:

1. **Telegram's ceiling for the transport** (it is a protocol limit, not a policy knob)
2. **Your host's disk** (a hosting constraint, reported separately)

| Route | Ceiling per file | Needs |
|---|---|---|
| Cloud Bot API (default) | **50 MB** | nothing |
| **MTProto with your bot token** — recommended | **2000 MB** | `api_id` + `api_hash` |
| Self-hosted Bot API server (`--local`) | 2000 MB | a second service + ~2× disk |

There is **no `MAX_UPLOAD_MB`**: nothing here caps a download or an upload, and no setting can
raise Telegram's own ceiling. What the bot *can* change is which protocol it uses — and the
recommended route needs no account, no phone number and no login code:

```dotenv
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

Those two values identify *your Telegram application* (from
[my.telegram.org](https://my.telegram.org) → *API development tools*); the `BOT_TOKEN` you
already have authenticates the bot. With them set, files over 50 MB go out over **MTProto**,
Telegram's own protocol, reaching **2000 MB per file** — the maximum a bot can ever send.
(Telegram Premium's 4 GB is for *people*, not bots, and this bot will not pretend otherwise.)

Small files keep using the Bot API, so nothing changes for the common case. `/limits`,
`/health`, the startup banner and `python main.py --check` all report the ceiling the
deployment really has.

### How the upload itself is sent

The large-file route is Pyrogram over MTProto, following the usual recipe:

```python
await client.send_video(chat_id, video=path, duration=…, width=…, height=…,
                        thumb=…, supports_streaming=True, progress=progress_for_pyrogram)
await client.send_audio(chat_id, audio=path, performer=…, title=…, progress=…)
await client.send_document(chat_id, document=path, progress=…)   # anything else
```

`MtprotoUploader.progress_for_pyrogram()` feeds percentage, transferred bytes, speed and
ETA into the same status message the download uses, throttled to Telegram's edit limit, and
a cancelled job aborts the upload from inside the callback. Files at or under 50 MB stay on
the Bot API, where python-telegram-bot does the same job with `sendVideo`/`sendAudio`/
`sendDocument`.

**Startup order matters.** The port is bound before Telegram is contacted, and the MTProto
client connects in the background: platforms (Render and friends) fail a deploy whose health
check does not answer in time, and a slow handshake must never decide whether the service
comes up.

**4000 MB?** Only a *user* account with Premium can, which would mean storing a login session
for a second account. This bot does not do that.

## Commands## Commands

| Command | What it does |
|---|---|
| `/start` | a picture, what the bot accepts (YouTube · torrent · direct), and shortcuts |
| `/help` | the three-step how-to |
| `/status` | running jobs, engine, transport, free disk |
| `/cancel` | stop the download, the muxing and the upload |
| `/limits` | the real ceiling, and how to raise it |
| `/id` | your chat id, for `OWNER_USER_ID` |
| `/about` | version and engine |
| `/diag` | **owner only** — host CPU budget, free disk, transports, fetcher, cached tracks, assets |

## Configuration

Everything has a default; `.env.example` documents all of it. The ones people actually change:

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | — | **required** |
| `OWNER_USERNAME` / `OWNER_USER_ID` | *(empty)* / `0` | the operator: `/diag`, the `/start` credit, cooldown-exempt. Everyone else can still download |
| `USER_COOLDOWN_SECONDS` | `5` | fair-use pause between two links from one user; `0` disables |
| `RICH_MESSAGES` | `true` | `/start` picture and animated progress cards |
| `ASSET_CACHE_PATH` | *(empty)* | where Telegram's file ids are cached; defaults to `<DOWNLOAD_DIR>/.asset-cache.json` |
| `WEBHOOK_URL` | *(empty)* | empty → long polling; on Render, `RENDER_EXTERNAL_URL` wins when the two disagree |
| `TELEGRAM_API_ID` / `TELEGRAM_API_HASH` | — | [my.telegram.org](https://my.telegram.org); unlocks 2000 MB uploads over MTProto |
| `UPLOAD_TRANSPORT` | `auto` | `auto`, `bot` (never use MTProto) or `mtproto` |
| `MT_CONNECTIONS` | `8` | parallel connections per upload (1–16) |
| `MIN_FREE_DISK_MB` | `256` | disk headroom kept before a job starts |
| `MAX_DURATION_MINUTES` | `0` | `0` = unlimited; refuses longer videos before downloading |
| `DEFAULT_QUALITY` | `720p` | the quality highlighted in green; `best` for the maximum |
| `TYPETUBE_API_KEY` | *(empty)* | lifts the cluster's quota and unlocks Hi-Fi audio |
| `ARIA2_BINARY` | `aria2c` | the fetcher for torrents, magnets and direct links |
| `TORRENT_METADATA_TIMEOUT` | `180` | seconds to wait for a magnet's file list |
| `ALLOW_PRIVATE_LINKS` | `false` | `true` lets the bot fetch loopback/private URLs — testing only |
| `PROGRESS_MIN_INTERVAL` | `5.0` | minimum seconds between status-message edits |
| `SHOW_OWNER_IN_START` | `false` | show the operator's handle on `/start` |
| `TYPETUBE_WORKERS` / `TYPETUBE_CHUNK_MB` | `0` / `10` | parallel range downloads; `0` = automatic (16 workers per CPU core, clamped to 8–32) |
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
                 ─▶ bot/upload.py             Bot API  (small files, ≤50 MB)
                                              MTProto    (big files, up to 2000 MB)
```

| Module | Responsibility |
|---|---|
| `config.py` | every setting, its default, and the capability each transport really has |
| `bot/typetube.py` | the engine: resolve, download, progress parsing, track cache |
| `bot/download.py` | download → verify → faststart → thumbnail, then check the file against Telegram |
| `bot/upload.py` | delivery, choosing the transport per file |
| `bot/mtproto.py` | the Pyrogram **bot** client that carries large uploads over MTProto |
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

* **Telegram decides the ceiling; this bot never invents one.** The Bot API stops bots at
  50 MB; MTProto with the same bot token reaches 2000 MB; a self-hosted local Bot API server
  also reaches 2000 MB. 4000 MB is a Premium *user* privilege, so the bot does not offer it.
  `/limits` always reports the number the active transport actually has.
* **YouTube only.** The engine treats anything else as a search query, so non-YouTube links
  are refused up front with an explanation rather than silently resolved to the wrong video.
* **Whatever the cluster can reach.** Region-locked, private or removed videos fail with a
  plain explanation; uploads are subject to Telegram's own rate limits.
* **Disk and RAM.** Downloads stream to disk, never through RAM, and each job needs room for
  the file plus its remux. `MIN_FREE_DISK_MB` (default 256) keeps a floor: a job that would
  cross it is refused up front, and partial files are always deleted.
* **Respect copyright.** Download what you own or are allowed to save.

## Documentation

| Document | Contents |
|---|---|
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | BotFather setup, Docker hosts, enabling uploads over 50 MB, day-two operations |
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | module-by-module walkthrough and design decisions |
| [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) | what each error means and how to fix it |
| [CONTRIBUTING.md](CONTRIBUTING.md) | how to run the checks before pushing |
| [CHANGELOG.md](CHANGELOG.md) | what changed, and why |
