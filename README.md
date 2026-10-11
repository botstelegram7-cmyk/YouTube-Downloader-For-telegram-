# YouTube Downloader for Telegram

A production-shaped Telegram bot that downloads YouTube videos and Shorts and delivers
them straight into your chat. The bot itself runs on **Telegram Serverless** — there is
no server for it — while a small **download worker** on
[Render](https://render.com) (Singapore) or [Voroa](https://getvoroa.com) (Mumbai) does
the downloading with [**TypeTube**](https://github.com/botstelegram7-cmyk/typetube),
falling back to yt-dlp only when TypeTube cannot deliver a file.

```
Telegram  ──update──▶  Serverless handler (tgcloud/)
                          │  validates the link, stores preferences,
                          │  submits an authenticated job
                          ▼
                       Download worker (worker/) — Render or Voroa
                          │  TypeTube: resolve → parallel range download → mux
                          │  fallback: yt-dlp
                          ▼
                       Telegram Bot API  ──▶  the video, in your chat
```

**New here? → [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** is a complete step-by-step
guide that runs entirely from Termux on an Android phone.

---

## Features

* **TypeTube-first downloads** — sub-second stream resolution, parallel range
  downloads (4–32 workers), automatic stream refresh when a CDN URL expires.
* **Honest fallback** — yt-dlp runs only when TypeTube fails, with the real reason
  reported to the user (private video, region block, age restriction…).
* **Automatic quality downgrade** — if a file would exceed Telegram's 50 MB bot-upload
  limit, the worker retries at the next smaller quality and, if it still cannot fit,
  offers one-tap "Retry 480p / 360p" buttons.
* **Cancel button** — long downloads can be stopped from the status message; the job's
  child processes and sockets are actually torn down.
* **Live progress** — the status message shows resolve → download percentage → mux →
  upload, throttled so Telegram's edit limits are never hit.
* **Secrets stay out of git** — the worker URL and shared secret live in the bot's
  Telegram database and are set once with `/setup`, which also deletes your message.
* **Abuse controls** — one job per user, a cooldown and a daily quota.
* **Audio mode** — `/audio` sends the soundtrack as M4A with proper title/performer
  metadata (transcoded only when the source is not already AAC).
* **Real tests** — the Serverless handlers run locally against a stub SDK
  (`tests/stubs/`), so the whole bot is covered without deploying.

## Requirements

| Piece | Requirement |
|---|---|
| Telegram bot | Created in [@BotFather](https://t.me/BotFather) with **Serverless enabled** |
| Node.js (for the CLI) | 18 or newer locally; the worker image pins **Node 24** |
| Worker host | Render (Singapore) or Voroa (Mumbai, India) — a free 512 MB instance is enough for personal use; see [the free plan notes](docs/DEPLOYMENT.md#free-plan-reality-check) and [the Voroa steps](docs/DEPLOYMENT.md#3d-on-voroa-mumbai-india) |
| Node.js (for TypeTube) | **22.15+ / 24** — TypeTube needs zlib zstd; see [TYPETUBE_COMPAT.md](docs/TYPETUBE_COMPAT.md) |

## Quick start

```bash
# 1. Bot handlers → Telegram Serverless
npm install
npx tgcloud login      # paste the CLI access token: @BotFather → your bot → Serverless → CLI Access
npm run check          # layout + syntax + all tests, before you deploy anything
npx tgcloud push
npx tgcloud migrate
npx tgcloud webhook

# 2. Worker → Render or Voroa
#    Render: Dashboard → New → Blueprint → pick this repository (render.yaml configures it)
#    Voroa:  New → Web Service → Runtime "Docker (Dockerfile)" → Dockerfile path /Dockerfile
#            (full runbook: docs/DEPLOYMENT.md → Step 3d)

# 3. Connect them, in Telegram
/setup https://your-worker.onrender.com YOUR_WORKER_SECRET
```

Full walkthrough with screenshots-in-words: **[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)**.

## Commands

| Command | What it does |
|---|---|
| `/start` | Introduction and the legal note |
| `/help` | Usage, limits and your current settings |
| `/quality` | Buttons to pick 360p–2160p (also `/quality 480p`) |
| `/audio` `/video` | Switch between audio-only (M4A) and video with sound |
| `/download 480p <link>` | One-off quality for a single link |
| `/status` | Worker health and your running job |
| `/cancel` | Stop your running download |
| `/about` | Credits, limits and sources |
| `/setup <url> <secret>` | **Owner** — connect the worker |
| `/config` | **Owner** — show the current configuration |
| `/stats` | **Owner** — worker queue, memory, TypeTube status |
| `/unset` | **Owner** — forget the worker credentials |

Sending a bare YouTube link (video, Shorts or `youtu.be`) downloads it with your saved
settings.

## Repository layout

```
tgcloud/                  Deployed to Telegram (the only folder that is)
├─ handlers/
│  ├─ message.js          All commands and link handling
│  └─ callback_query.js   Inline buttons: quality, cancel, retry smaller
├─ lib/
│  ├─ config.js           Non-secret settings (owner id, limits, defaults)
│  ├─ youtube.js          URL/command logic, shared with the worker via test fixtures
│  ├─ messages.js         Every user-facing string
│  ├─ storage.js          Database access (settings, preferences, jobs)
│  ├─ worker_api.js       Authenticated client for the worker
│  └─ download_flow.js    The single path every download takes
└─ schema.js              user_preferences, bot_settings, download_jobs

worker/                   Deployed to Render or Voroa
├─ src/
│  ├─ index.js            Boot, health endpoint, graceful shutdown
│  ├─ server.js           HTTP surface: /download, /jobs/:id/cancel, /health, /stats
│  ├─ queue.js            Concurrency limits, one job per chat, cancellation
│  ├─ downloader.js       TypeTube-first with downgrade ladder and normalised results
│  ├─ typetube.js         Runtime guard for TypeTube (the zstd/Node issue)
│  ├─ ytdlp.js            Fallback engine with progress parsing
│  ├─ telegram.js         Bot API client: JSON calls + streamed multipart uploads
│  ├─ job.js              One job end to end: status, download, upload, cleanup
│  ├─ ffmpeg.js           Mux/convert helpers
│  ├─ thumbnail.js        Preview thumbnails for sendVideo
│  └─ validate.js         Shared URL/quality rules (mirrors tgcloud/lib/youtube.js)
├─ test/                  Unit tests (node:test)
└─ Dockerfile             Node 24 + FFmpeg + yt-dlp (subdirectory build)

Dockerfile                Same image built from the repository root, for hosts that
                          build the root and look for /Dockerfile (e.g. Voroa)
.node-version             Pins Node 24 so buildpack hosts do not fall back to Node 20

tests/                    Tests for the Serverless side
├─ handlers.test.mjs      Real handlers against a stub SDK
├─ youtube.test.mjs       URL rules using the shared fixture file
├─ worker_api.test.mjs    Retry/backoff and error mapping
├─ structure.test.mjs     Platform layout rules
└─ stubs/                 In-memory SDK + module resolution hook

docs/                     DEPLOYMENT · ARCHITECTURE · TROUBLESHOOTING · TYPETUBE_COMPAT
render.yaml               Render Blueprint for the worker
```

## Testing

```bash
npm run check        # everything: modules, syntax, bot tests, worker tests
npm test             # Serverless side only (69 tests)
npm run test:worker  # worker only (62 tests)
npm run check:modules
```

The Serverless side is tested by resolving `sdk` to an in-memory stub:

```bash
node --import ./tests/stubs/register.mjs --test tests/
```

That means `sgcloud`/`tgcloud` handler logic — quotas, buttons, config, error copy —
is covered without touching Telegram.

## How it works, in detail

* [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) — components, request lifecycle, why the
  worker exists, failure modes, and the one limitation worth knowing (the worker
  cannot write into the bot's database).
* [docs/TYPETUBE_COMPAT.md](docs/TYPETUBE_COMPAT.md) — the Node/zstd requirement, the
  bug in `typetube@1.1.4` it works around, and what to do on each runtime.
* [docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md) — symptom → cause → fix.

## Limits and legal notes

* Telegram's hosted Bot API allows **50 MB per uploaded file**. Larger videos need a
  lower quality; a self-hosted Bot API server raises this ceiling and `MAX_UPLOAD_MB`
  exists for that case.
* TypeTube's public cluster allows **60 resolves/minute and 1,500/day per IP address**.
  A private key raises the quota and unlocks 256 kbps audio.
* Some music/VEVO tracks are restricted by YouTube to the cluster's region and will
  fail with HTTP 403. Hosting the worker in Singapore gives the best results.
* **Download only what you own or are authorised to save.** Respect copyright,
  YouTube's Terms of Service and your local law. The bot repeats this on `/start`,
  `/about` and `/help`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Security reports: [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE)
