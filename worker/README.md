# Download worker

The Node service that actually downloads videos: **TypeTube first**, yt-dlp as a
fallback, FFmpeg for muxing, then upload to Telegram.

It exists because Telegram Serverless gives you a V8 sandbox with no filesystem, no npm
packages and no TCP sockets — none of which TypeTube can run without. See
[docs/ARCHITECTURE.md](../docs/ARCHITECTURE.md) for the full picture.

## Run it locally

```bash
cd worker
npm install
cp .env.example .env      # fill in BOT_TOKEN and WORKER_SECRET
node --env-file=.env src/index.js
```

`--env-file` needs Node 20.6+; otherwise export the variables in your shell. Then:

```bash
curl -s http://127.0.0.1:8080/health
curl -s -H "Authorization: Bearer $WORKER_SECRET" http://127.0.0.1:8080/stats
```

Submit a real job (this downloads and uploads to your chat):

```bash
curl -s -X POST http://127.0.0.1:8080/download \
  -H "content-type: application/json" \
  -H "Authorization: Bearer $WORKER_SECRET" \
  -d '{"url":"https://youtu.be/jNQXAC9IVRw","chat_id":<your chat id>,"status_message_id":<a message id>,"quality":"360p","mode":"video"}'
```

`status_message_id` must be a message the bot may edit — easiest is to send `/start` to
your bot and use the message id of its reply.

## Tests

```bash
npm test     # 62 tests, no network
npm run lint # syntax + hygiene
```

### Verify the whole pipeline locally

`scripts/e2e_local.mjs` starts a fake Telegram Bot API, boots this worker against it and
runs a real download, so you can check a change to the download or upload path without a
bot token or a deployment:

```bash
node scripts/e2e_local.mjs                                      # 20-second test video, 360p
node scripts/e2e_local.mjs https://youtu.be/XXXXXXXXXXX 720p audio
FFMPEG_BINARY=/path/to/ffmpeg node scripts/e2e_local.mjs        # if ffmpeg is not on PATH
```

It prints the health report, every status edit the worker made, the upload method and
size, and ends with `✅ PASS` when the file was delivered.

### Self-hosted Bot API server (optional)

`MAX_UPLOAD_MB` defaults to **0 — no limit on the worker's side**. The only ceiling left is
Telegram's: the hosted Bot API refuses bot uploads above 50 MB, and no worker setting can
change that. To actually send larger files, run a
[self-hosted bot API server](https://github.com/tdlib/telegram-bot-api), set
`TELEGRAM_API_BASE=http://127.0.0.1:8081` and raise `MAX_UPLOAD_MB` to `2000` (the hard
limit per file). Uploads follow the scheme of that URL, so a local server on plain http
works.

Setting `MAX_UPLOAD_MB` to a number instead switches the automatic downgrade back on: the
worker re-downloads at a lower quality when the result would not fit.

## Environment variables

Every variable is validated at boot; a bad value produces one readable list of problems.
The full reference with defaults lives in [`.env.example`](.env.example). The ones that
actually matter on a small instance:

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | — | BotFather bot token, used for uploads and status edits |
| `WORKER_SECRET` | — | ≥ 16 chars; generate with `openssl rand -hex 32` |
| `PORT` | `8080` | Render injects its own value |
| `MAX_CONCURRENT_DOWNLOADS` | `1` | Keep 1 on 512 MB |
| `DOWNLOAD_WORKERS` | `4` | TypeTube range workers |
| `CHUNK_SIZE_MB` | `8` | Range chunk size |
| `MAX_UPLOAD_MB` | `0` | Worker-side upload cap; `0` = unlimited (Telegram still caps at 50 MB unless you self-host the bot API) |
| `AUTO_DOWNGRADE` | `true` | Retry smaller when the result would not fit |
| `YTDLP_FALLBACK` | `true` | Use yt-dlp when TypeTube fails |
| `STRICT_STARTUP` | `false` | Refuse to boot when TypeTube cannot run on this Node |

## HTTP API

| Method | Route | Auth | Purpose |
|---|---|---|---|
| GET | `/` | no | Service banner |
| GET | `/health` | no | Liveness + capabilities (used by Render's health check) |
| GET | `/stats` | Bearer | Queue, memory, uptime, TypeTube detail |
| POST | `/download` | Bearer | Queue a job → `202 { job_id, position, running }` |
| POST | `/jobs/:id/cancel` | Bearer | Stop a job (idempotent) |

Status codes: `400` invalid payload (with a `problems` array), `401` bad secret,
`409` that chat/user already has a job, `413` body too large, `503` queue full.

## Docker

```bash
docker build -t yt-worker .
docker run --rm -p 8080:8080 \
  -e BOT_TOKEN=... -e WORKER_SECRET=... \
  yt-worker
```

The image pins **Node 24** and installs FFmpeg + yt-dlp. The Node version is not
negotiable — TypeTube needs zlib zstd. See
[docs/TYPETUBE_COMPAT.md](../docs/TYPETUBE_COMPAT.md).

## Deploying

The repository root ships a Render Blueprint ([`render.yaml`](../render.yaml)) that
configures this service, including the free-instance-safe defaults. Step-by-step
instructions, including the keep-alive cron for the free plan:
[docs/DEPLOYMENT.md](../docs/DEPLOYMENT.md).
