# Deployment guide

The bot is a single Python web service: FFmpeg, Node.js (the TypeTube engine) and python-telegram-bot inside one
container. Nothing else is needed — there is no worker, no database and no serverless
account.

This guide walks through every supported route, in the order most people want them:

1. [Step 1 — Create the bot with BotFather](#step-1--create-the-bot-with-botfather)
2. [Step 2 — Try it locally first](#step-2--try-it-locally-first)
3. [Step 3 — Deploy as a Web Service](#step-3--deploy-as-a-web-service)
   - [3a. Render (blueprint, one click)](#3a-render-blueprint-one-click)
   - [3b. Voroa](#3b-voroa)
   - [3c. Any Docker host (Fly.io, Railway, a VPS)](#3c-any-docker-host)
4. [Step 4 — Uploads larger than 50 MB (bot-token MTProto)](#step-4--uploads-larger-than-50-mb-bot-token-mtproto)
5. [Step 5 — Age-restricted or sign-in-only videos (cookies)](#step-5--age-restricted-or-sign-in-only-videos-cookies)
6. [Step 6 — Keep a free instance awake](#step-6--keep-a-free-instance-awake)
7. [Step 7 — Day-two operations](#step-7--day-two-operations)
8. [Free-plan reality check](#free-plan-reality-check)

---

## Step 1 — Create the bot with BotFather

```
/newbot
  → name:      YouTube Downloader
  → username:  something_ending_in_bot
  → copy the token: 123456789:AAH...
```

Then, in the same chat:

```
/setcommands      → paste the list from BOT_PROFILE.md
/setdescription   → paste the description from BOT_PROFILE.md
/setabouttext     → optional, same file
```

Get **your** numeric id: open a chat with the bot once it is running and send `/id`, or ask
[@userinfobot](https://t.me/userinfobot). You need it for `OWNER_USER_ID` (locks the bot to
you) and `STARTUP_NOTIFY_CHAT_ID` (startup introduction).

> **Keep the token secret.** It is the bot's password. If it ever leaks, `/revoke` in
> BotFather issues a new one — then update the environment variable and redeploy.

---

## Step 2 — Try it locally first

Faster than debugging in the cloud, and it proves the toolchain:

```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

export BOT_TOKEN="123456789:AAH..."
python main.py --check          # config + FFmpeg + disk + versions in one screen
python main.py                  # no WEBHOOK_URL set → long polling, no ports to open
```

Send a link; press a quality button; watch the status message. `Ctrl+C` stops it cleanly.

If you have Docker, the same thing with one line — set `TELEGRAM_API_ID` and
`TELEGRAM_API_HASH` and uploads reach 2000 MB with no account of any kind:

```bash
cp .env.example .env && $EDITOR .env      # BOT_TOKEN, TELEGRAM_API_ID, TELEGRAM_API_HASH, OWNER_USER_ID
docker compose up --build                 # add -d to detach
docker compose logs -f bot
```

Leave `WEBHOOK_URL` empty on a laptop: the bot then uses long polling, which needs no
public URL and no inbound port.

---

## Step 3 — Deploy as a Web Service

Whatever the host, the shape is the same:

| Setting | Value |
|---|---|
| Build | Docker, `Dockerfile` at the repository root |
| Start command | `python main.py` (already the image's `CMD`) |
| Port | from `PORT` (the bot binds `LISTEN_HOST:$PORT`, default `0.0.0.0:8080`) |
| Health check | HTTP `GET /health` — returns `503` when FFmpeg is missing |
| Webhook URL | leave `WEBHOOK_URL` unset if the platform exposes a public URL automatically (`RENDER_EXTERNAL_URL` is detected) or set it explicitly |

Required environment variables: **`BOT_TOKEN`**. Recommended: `OWNER_USER_ID`,
`STARTUP_NOTIFY_CHAT_ID`, `WEBHOOK_SECRET`, `MAX_CONCURRENT_JOBS=1` on small instances.

### 3a. Render (blueprint, one click)

1. Push the repository to GitHub (or use the existing one).
2. Render Dashboard → **New → Blueprint** → select the repository.
   Render reads [`render.yaml`](../render.yaml): a Docker web service on the free plan in
   Singapore, health check `/health`, `WEBHOOK_SECRET` auto-generated.
3. When it asks, set **`BOT_TOKEN`** (it is declared `sync: false`, so it is never stored
   in git). Optionally set `STARTUP_NOTIFY_CHAT_ID`.
4. Deploy. The build installs FFmpeg and the Python dependencies; the first boot prints the
   banner in the logs and — if `STARTUP_NOTIFY_CHAT_ID` is set — sends the introduction to
   your Telegram chat.
5. Wait for the service to read **Live**. Render uses `/health` to decide that, so a build
   without FFmpeg fails here instead of going live.
6. Send the bot a link.

The free plan caps uploads at 50 MB, because the service talks to Telegram's cloud Bot API.
For 2000 MB, see [Step 4](#step-4--uploads-larger-than-50-mb-the-local-bot-api-server) —
that path needs a host where you can run a second container.

### 3b. Voroa

New → **Web Service** → runtime **Docker**, Dockerfile path `/Dockerfile` (fixed when the
service is created — check it before you click deploy), port `3000` if the platform insists
on its own value: set `PORT=3000` and the bot will bind that instead.

Then set the environment variables from the table above, deploy, and confirm the service is
live. Voroa also relies on the declared `HEALTHCHECK` in the image, which is already there.

### 3c. Any Docker host

```bash
docker build -t ytbot .
docker run -d --name ytbot --restart unless-stopped \
  -p 8080:8080 \
  -e BOT_TOKEN="123456:ABC..." \
  -e WEBHOOK_URL="https://bot.example.com" \
  -e WEBHOOK_SECRET="$(openssl rand -hex 16)" \
  -e OWNER_USER_ID=123456789 \
  -v ytbot-downloads:/tmp/ytbot-downloads \
  ytbot
```

Put it behind a TLS terminator (Caddy, nginx, a load balancer). Telegram only delivers
webhooks over HTTPS. Point the proxy at `8080` and forward `/telegram` (or whatever
`WEBHOOK_PATH` says) to the container without rewriting the path — the bot validates the
path and the `X-Telegram-Bot-Api-Secret-Token` header itself.

Prefer polling if you cannot expose an HTTPS endpoint: set `USE_POLLING=true` (or simply
leave `WEBHOOK_URL` empty). Long polling needs only outbound connections; it is slightly
less snappy but works from a home network or a container with no ingress.

---

## Step 4 — Uploads larger than 50 MB (bot-token MTProto)

Telegram's Bot API refuses **any** bot upload above 50 MB — that ceiling is Telegram's, and no
setting in this application can change it. Telegram's own protocol, **MTProto**, allows a bot
to send **2000 MB** per file, and it needs nothing you do not already have:

```dotenv
TELEGRAM_API_ID=1234567
TELEGRAM_API_HASH=0123456789abcdef0123456789abcdef
```

1. Take both values from [my.telegram.org](https://my.telegram.org) → *API development tools*.
   They identify **your Telegram application**; they are not a login and they authorise nothing
   on their own.
2. Put them in the service's environment and restart. Your existing `BOT_TOKEN` authenticates
   the bot over MTProto exactly as it does over the Bot API.

That is the whole setup. No second account, no phone number, no login code, **no session
string to store or leak** — which is why this is the recommended route.

Confirm it before trusting it:

```bash
python main.py --check     # prints "MTProto : ready — bot token + API ID/hash, no user login"
```

The startup banner then reads `Upload : up to 2000 MB per file (MTProto with your bot token)`,
and the log line `Large uploads enabled: MTProto as @yourbot, up to 2000 MB per file` confirms
the client connected. If it cannot connect, that is logged as a warning and the bot keeps
running on the Bot API — a broken optional transport must never take the bot offline.

Practical notes:

* **Files under 50 MB still go through the Bot API.** MTProto wakes only when the file needs it,
  so ordinary links see no change at all.
* **4000 MB is not available to bots.** That is Telegram Premium for *user* accounts; offering it
  would mean storing somebody's login session. This bot does not do that.
* **Disk.** Downloads are streamed to disk (never through RAM) and each job needs room for the
  file plus its remux. `MIN_FREE_DISK_MB` (default 256) is the floor: a job that would cross it is
  refused up front, and partial files are deleted on every failure path.
* **Rate limits.** Telegram may answer `FLOOD_WAIT` on a busy day; the bot reports the wait plainly
  instead of retrying in a loop.
* **`MT_CONNECTIONS`** (default 4, maximum 8) tunes how many parallel connections carry an upload.

### Alternative: a self-hosted Bot API server

If you would rather keep everything on the Bot API, run Telegram's own server with `--local` and
point `TELEGRAM_API_BASE` at it — the ceiling becomes 2000 MB and **all** API calls go through it.
`docker-compose.yml` can do this (`docker compose --profile local-api up -d --build`); it costs a
second container and roughly twice the largest file in scratch space. The bot detects it, reports
`local Bot API server` in the banner, and `/limits` explains the ceiling.

## Step 5 — What the engine cannot fetch

The TypeTube cluster downloads as an anonymous client, so **private, members-only and
age-restricted videos are not available**: there is no cookie or login to supply. The bot says
so plainly when the engine reports it (`That video is unavailable, private or removed.`)
instead of retrying forever.

If you need those videos, run your own pipeline rather than pointing this bot at an account —
and note that YouTube's terms apply to whatever you build.

## Step 6 — Keep a free instance awake

Free tiers (Render, Voroa) sleep after ~15 minutes without traffic and take 30–60 seconds
to wake. In webhook mode that means the first message after a quiet period waits for the
container to come back.

The documented workaround is a free uptime monitor pinging a cheap endpoint every 5–10
minutes:

1. Create a monitor at [UptimeRobot](https://uptimerobot.com) or [cron-job.org](https://cron-job.org).
2. URL: `https://<your-service>/health` — it is unauthenticated by design and answers in
   milliseconds without touching the bot.
3. Interval: 5 minutes.

`/health` returns `503` if FFmpeg is missing, so the monitor doubles as a real health
check. Keep the 750 instance-hours/month free-plan budget in mind: a service that never
sleeps uses the whole allowance in a month (24 × 31 = 744 h).

---

## Step 7 — Day-two operations

**Watch the logs**

```bash
docker compose logs -f bot          # or: render logs / your host's log viewer
```

The bot logs one line per meaningful event: link received, resolve result, job started,
quality chosen, download finished, upload method, cleanup — with a job id on every line of
a job, so grep is enough:

```bash
docker compose logs bot | grep "Job a1b2c3d4"
```

**Check the live state**

```bash
curl -s https://<your-service>/health | jq      # ffmpeg version, upload limit, mode, uptime
curl -s https://<your-service>/stats  | jq      # active jobs per user, queue, counters
```

**Update the bot**

```bash
git pull
docker compose up -d --build      # Render: Manual Deploy → Deploy latest commit
```

There is no migration step and no database to upgrade.

**Keep the engine fresh.** The TypeTube CLI is pinned in the Dockerfile; YouTube changes
constantly, so a newer CLI is usually the fix for sudden extraction failures.

```bash
# bump ARG TYPETUBE_VERSION in the Dockerfile (and NODE_VERSION if required), then:
docker compose up -d --build
```

**Change a knob.** Edit the environment variable in the dashboard (or `.env`) and restart.
`MAX_CONCURRENT_JOBS`, `DEFAULT_QUALITY`, `MAX_DURATION_MINUTES`, `MIN_FREE_DISK_MB`,
`UPLOAD_TRANSPORT`, `LOG_LEVEL` are the usual ones. There is no `MAX_UPLOAD_MB` any more:
file size is Telegram's ceiling, not a setting.

**Rotate the webhook secret.** Set a new `WEBHOOK_SECRET` and restart; the bot re-registers
the webhook at startup, so nothing else is needed.

**Move the bot to another host.** Only the environment variables carry state — the bot token and
`api_id`/`api_hash`, with nothing to export or re-login. If you use the optional local Bot API
server, its `telegram-bot-api-data` volume travels with it.

---

## Free-plan reality check

| | Render free | Voroa free |
|---|---|---|
| RAM / CPU | 512 MB / 0.1 vCPU | free Nano tier (small) |
| Sleep | after ~15 min idle | after ~15 min idle |
| Cold start | 30–60 s | similar |
| Upload ceiling | 50 MB by default; 2000 MB with `TELEGRAM_API_ID`/`HASH` | same |
| Practical use | 1 job at a time, 360p–720p, short clips | same |

A 512 MB instance handles one 720p job comfortably, and two only if the videos are short.
ffprobe/FFmpeg are the memory hogs, and a 1080p remux on 0.1 vCPU is slow. If you need
more, the cheapest real fix is a €4 VPS: run `docker compose up -d` there with the local
Bot API server enabled and get 2000 MB uploads, no sleep and no cold starts.
