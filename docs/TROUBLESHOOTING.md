# Troubleshooting

Symptom → cause → fix, for the things that actually go wrong. Start with the two commands
that answer most questions at once:

```bash
python main.py --check          # config, FFmpeg, disk, versions — prints exactly what it finds
curl -s https://<your-service>/health | jq
docker compose logs --tail=100 bot   # or your host's log viewer
```

Every log line of a job carries its job id (`Job a1b2c3d4 …`), so `grep a1b2c3d4` prints
the whole story of one download.

---

## Contents

- [The bot does not answer](#the-bot-does-not-answer)
- [The container exits or restarts in a loop](#the-container-exits-or-restarts-in-a-loop)
- [Health, webhook and polling](#health-webhook-and-polling)
- [Links are rejected](#links-are-rejected)
- [Probing fails](#probing-fails)
- [Downloads fail](#downloads-fail)
- [Uploads fail or stall](#uploads-fail-or-stall)
- [The delivered file is wrong](#the-delivered-file-is-wrong)
- [Cancellation, cleanup and disk](#cancellation-cleanup-and-disk)
- [Local Bot API server (2 GB mode)](#local-bot-api-server-2-gb-mode)
- [Local development and tests](#local-development-and-tests)
- [Clean slate](#clean-slate)

---

## The bot does not answer

**1. Is the process alive and reachable?**

```bash
curl -s https://<your-service>/health          # 200 + JSON
```

No response → the service is not running (see the next section). A response with
`"ffmpeg": null` and status **503** → the container is up but FFmpeg is missing.

**2. Is the token right?** `python main.py --online` calls `getMe`. `401 Unauthorized`
means the token is wrong or revoked; fix `BOT_TOKEN` and restart. `main.py --check` catches
the shape of a token (a `123:ABC` pattern) but only Telegram can confirm it is live.

**3. Is the bot locked to someone else?** With `OWNER_USER_ID` set, other users are
ignored by design — they get no answer at all. Send `/id` to check your numeric id.

**4. Polling or webhook?** The startup banner states the mode:

```
  Mode       : webhook → https://mybot.onrender.com/telegram
  Mode       : polling
```

In webhook mode the bot registers the webhook at startup; until that line appears in the
log, Telegram is not sending anything. In polling mode, look for the opposite problem:
`Conflict: terminated by other getUpdates request` means **two instances are polling the
same token**, or a webhook is still registered. One instance only, then:

```bash
curl -s "https://api.telegram.org/bot<TOKEN>/deleteWebhook?drop_pending_updates=true"
```

**5. Did the updates ever arrive?** With `LOG_LEVEL=DEBUG` you get one line per update.
No lines at all → the problem is above the bot (token, webhook, network), not inside it.

---

## The container exits or restarts in a loop

`python main.py` fails fast and loudly on purpose — a misconfigured bot should not pretend
to run. The reason is the last line before the exit:

| Log line | Fix |
|---|---|
| `ConfigError: BOT_TOKEN is required` | Set `BOT_TOKEN`. |
| `ConfigError: … port` out of range | The setting is outside its allowed range; `/health` and the banner print the effective values. |
| `MissingDependency: FFmpeg was not found …` | Install FFmpeg or point `FFMPEG_BINARY`/`FFPROBE_BINARY` at it. |
| `CommandError: ffprobe failed: …` at boot | The binary exists but cannot execute (wrong architecture, missing shared library). Test with `ffprobe -version` inside the container. |
| `Could not write to the download directory` | `DOWNLOAD_DIR` is not writable by the container user (uid 10001). Mount a writable volume or change the path. |
| Repeated `TelegramError` during startup | The bot cannot reach the API — a blocking firewall, a bad proxy, or (for the local API server) a wrong `TELEGRAM_API_BASE`. |

`python main.py --check` reports all of these before the bot even starts trying to connect.

---

## Health, webhook and polling

**`/health` returns 503.** Read the JSON: it names the failing dependency. The usual case
is `"ffmpeg": null` — the image has FFmpeg, so this normally means a custom build or a
`FFMPEG_BINARY` pointing at a path that does not exist. The health endpoint intentionally
reports this as *unhealthy*: the bot cannot do its job without it.

**"Wrong response from the webhook: 403 Forbidden".** The `X-Telegram-Bot-Api-Secret-Token`
header did not match `WEBHOOK_SECRET`. Set the same value in the bot and re-register (it
happens automatically at startup) — or clear `WEBHOOK_SECRET` on both sides. This is also
what you are protecting against: someone else POSTing fake updates to your endpoint.

**404 on the webhook path.** `WEBHOOK_PATH` (default `telegram`) is the last segment of the
URL Telegram calls: `https://host/telegram`. If a proxy strips or rewrites the path, the
bot answers 404 — disable path rewriting for that route, or change `WEBHOOK_PATH`.

**Telegram requires HTTPS.** A webhook URL must be `https://`; in practice this means a
TLS terminator in front of the container. No public HTTPS? Use polling: leave `WEBHOOK_URL`
empty or set `USE_POLLING=true`. Polling needs no ingress and behaves identically for the
user, with a small added latency.

**Updates are rejected with 413.** The bot refuses webhook payloads larger than 2 MB on
purpose. Telegram never sends anything near that; a 413 means something else is posting to
the endpoint (a health checker with a huge body, or an attacker). Check the caller, and keep
`WEBHOOK_SECRET` set.

---

## Links are rejected

**"🚫 That link is not allowed — private address".** The SSRF guard blocked a host that
resolves to loopback, private (`10.*`, `192.168.*`), link-local (`169.254.*` — the cloud
metadata address) or otherwise internal range. That guard is deliberate: without it, a
public bot can be used to make your server fetch its own internal network. It applies to
the DNS result too, not just the URL text.

**I really do want a local URL** (a LAN media server, or the bundled e2e test): set
`ALLOW_PRIVATE_URLS=true`. That is a *testing* switch — expect an internal-network attacker
surface if you leave it on in production.

**"🤔 That does not look like a link".** Only `http(s)` URLs are accepted. Bare words,
`ftp://`, `file://` and pasted text are refused.

**Playlist links** are refused unless `ALLOW_PLAYLIST=true`, and even then only the first
item is downloaded — a bot that silently starts 400 downloads is not a feature.

---

## Probing fails

Resolving means the TypeTube CLI reporting a link's streams without downloading (45 s limit;
`INSPECT_TIMEOUT` in `bot/handlers/links.py`). The failure text in chat lists the usual
causes; in the log, one of these appears:

| Log line | Meaning and fix |
|---|---|
| `probe timed out` | The site is slow or throttling. Try again; always-on hosts with a distant region (a US datacentre for an Indian-only site) are the usual culprit. Consider `PROXY`. |
| Not a YouTube link | The bot refuses non-YouTube URLs before the engine sees them, and says which host it rejected. |
| Private, members-only or age-restricted | The cluster fetches anonymously, so these are unavailable; the bot explains instead of retrying. |
| `HTTP Error 403` | Region/IP block — a proxy is the usual answer. |
| `No downloadable formats` | It is a live stream, a text page, or a page whose media loads only after login. |

**A stale engine is the most common cause of sudden resolve failures.** YouTube changes
constantly; bump `TYPETUBE_VERSION` in the Dockerfile and rebuild.

---

## Downloads fail

**"Requested format is not available".** The bot already guards against this: every
format selector ends in a plain `best` fallback, so a pick can never fail merely because
the source's metadata was thin (direct media URLs, unusual extractors). If you still see it,
the source changed between the probe and the download — send the link again.

**The download stops at a random percentage.** Check the log for the engine's own `[Error]` line:
`HTTP Error 429` (rate-limited — wait, or set `PROXY`), `Connection reset` (retried
automatically `YTDLP_RETRIES` times), or `read timed out` (raise
`YTDLP_SOCKET_TIMEOUT`). The job fails cleanly and its temp files are removed.

**It never finishes.** `DOWNLOAD_TIMEOUT_SECONDS` (default 3600) ends the job with a clear
message. On a 0.1 vCPU free tier, a long 1080p download can be starved by the rest of the
system; lower `MAX_CONCURRENT_JOBS` to 1 and prefer smaller qualities.

**Disk full during a download.** Every job has its own `job-<id>-*` directory under
`DOWNLOAD_DIR`, deleted when the job ends. Free space is reported by `/status` and in the
startup log. Give the host about twice the size of the largest file you intend to deliver.

---

## Uploads fail or stall

**"That file is too large".** The bot refuses *after* downloading, when the finished file
exceeds what Telegram will take from this bot — never before, because there is no size cap in
this application to check against. Read the message: it names the ceiling and why. The number
comes from the transport in use — **50 MB on the cloud Bot API, 2000 MB over MTProto with your
bot token, 2000 MB with a local Bot API server** (`TELEGRAM_API_BASE`). To raise it, set
`TELEGRAM_API_ID` and `TELEGRAM_API_HASH`; nothing you can set will ever *lower* it. `/limits`
explains this to users, and the startup banner prints it for operators.

> A file that fits under the ceiling can still be refused by Telegram itself (for example
> 50 MB exactly, with multipart overhead). Telegram's error is passed through verbatim.

**The upload stalls at some percentage.** Telegram throttles large uploads; the bot honours
`RetryAfter`/`FLOOD_WAIT` and reports how long Telegram asked it to wait. A stall that never recovers
usually means a proxy/load balancer cutting long-lived connections — the upload timeout is
generous (`1.5 s per MB`, minimum 10 minutes) by design. Check the host's request timeout
settings before blaming the bot.

**`sendVideo` was refused (falling back to `sendDocument`).** This is in the log on
purpose: Telegram does not consider that file a playable video (an unusual container or
codec, or a file that is not really a video). The same bytes are delivered as a document so
the user still gets the file. If it happens for a common format, FFmpeg probably remuxed
into a container the client does not accept — the file itself is fine.

**`Forbidden: bot was blocked by the user`.** The user blocked the bot mid-job. The job
stops; nothing to fix.

---

## The delivered file is wrong

**No sound.** The chosen quality was video-only (normal for 1080p+ on many sites) and the
audio stream could not be muxed. The log says which FFmpeg command failed. Common causes:
a source whose audio the engine could not fetch (retry), or a full disk during
muxing. `Video has no audio track` in the log means the *source* had none — that is on the
source, not the bot.

**No thumbnail.** Thumbnails are generated with FFmpeg from a frame about 20% into the
video (JPEG, ≤200 kB, longest side ≤ `THUMBNAIL_MAX_SIDE` = 320). If generation fails, the
file is still delivered and the log explains why — for example a video shorter than the
seeking point or a codec FFmpeg cannot decode.

**The video does not stream / takes long to start.** MP4 files are remuxed with
`+faststart` when they are not already faststart (the log reports
`faststart=True`). Delivered files always have it; if a client still downloads fully first,
it is the client.

**The caption is cut off.** Captions are capped at 1000 characters (Telegram allows 1024,
and the bot leaves room for the source link). Long titles are truncated with an ellipsis.

---

## Cancellation, cleanup and disk

**`/cancel` says "Nothing is running for you right now."** The job already finished,
failed or was auto-cancelled by a newer request. `/status` shows what is actually running.

**A child process survived a cancel.** Every engine/FFmpeg process is registered in
`ProcessRegistry`, which sends `SIGTERM`, waits briefly, then `SIGKILL`s whatever is still
alive. If a process is stuck in uninterruptible I/O (a dead NFS mount, a wedged disk), even
`SIGKILL` cannot help — restart the container.

**Temp files are accumulating.** Check `CLEANUP_AFTER_UPLOAD` (default `true`) and
`KEEP_FAILED_FILES` (default `false`). Orphaned directories can also be left by a
`SIGKILL`ed container: they are safe to delete, since each job works in its own directory
and only deletes its own:

```bash
docker exec -it ytbot sh -c 'ls -la /tmp/ytbot-downloads && rm -rf /tmp/ytbot-downloads/job-*'
```

**The user's previous job vanished.** That is the feature: one job per user. A new link
cancels the running one, rewrites its status message to "♻️ Cancelled your previous request
to start the new one." and deletes its temp directory.

**Jobs queue up and nothing seems to run.** `MAX_CONCURRENT_JOBS` (default 2) caps how many
jobs run at once; the rest wait their turn and the status message says so. On a 512 MB
instance set it to 1.

---

## Local Bot API server (2 GB mode)

**`/health` and the banner still say 50 MB.** The bot did not detect a local API server:
check `TELEGRAM_API_BASE`. It must point at the server (for example
`http://telegram-bot-api:8081`) and must not be `https://api.telegram.org`.

**The bot cannot reach it.** Inside compose, `TELEGRAM_API_BASE` uses the *service name*
`telegram-bot-api`, not `localhost` — each container has its own loopback. Test from the
bot container:

```bash
docker compose exec bot python -c "import os,urllib.request;print(urllib.request.urlopen('http://telegram-bot-api:8081/bot'+os.environ['BOT_TOKEN']+'/getMe').read().decode()[:120])"
```

**The server container exits immediately.** `TELEGRAM_API_ID` and `TELEGRAM_API_HASH` are
mandatory — get them from [my.telegram.org](https://my.telegram.org) → *API development
tools* (a phone-based login, unrelated to the bot token). Without them the official server
refuses to start. Its logs say so.

**Uploads still fail at 50 MB even with the local server.** The `--local` flag comes from
`TELEGRAM_LOCAL=1` in `docker-compose.yml`; without it the server still enforces 50 MB.

**A 2000 MB upload dies halfway.** Disk space is the usual cause: the server spools every
upload to disk before sending it (budget ~2× the file size), and a 512 MB/0.1 vCPU free
instance is not a realistic host for that workload.

**Ports and domains.** The bot listens on `PORT` (default 8080); the API server listens on
8081 *inside the compose network* and does not need to be published to the internet. Only
the bot's webhook port must be reachable from Telegram.

---

## Local development and tests

**`ModuleNotFoundError: No module named 'aiohttp'` (or `telegram`).** Install the
dependencies, not just the source:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
```

The end-to-end harness needs `aiohttp`, which is part of `requirements.txt`.

**pytest skips the FFmpeg tests.** By design: those tests need real FFmpeg/ffprobe and skip
themselves when neither is on `PATH` (the earlier suites still run):

```
70 passed                      # everything ran
63 passed, 7 skipped           # no FFmpeg installed
```

Point the tests at a build explicitly if you keep one outside `PATH`:

```bash
export FFMPEG_BINARY=/opt/ffmpeg/ffmpeg
export FFPROBE_BINARY=/opt/ffmpeg/ffprobe
pytest -q
```

**The end-to-end harness fails immediately with "FFmpeg not found".** Pass the path:

```bash
python scripts/e2e_local.py --ffmpeg /opt/ffmpeg        # directory containing ffmpeg+ffprobe
python scripts/e2e_local.py --keep-files                # leave the workspace for inspection
```

It starts its own mock Bot API, a media server and the real bot, so **the ports 8091/8092/8093
must be free** (change them with `--api-port`, `--media-port`, `--bot-port`).

**The harness times out waiting for the quality keyboard.** Read the bot log it prints —
that is the whole point of the harness. A common local cause is another process already
listening on a port, or a firewalled loopback.

**The harness says "the bot exited with code N before serving /health".** The bot never
started: the message names the exit code and the repository root it used. If the root is
wrong (the harness looks for `main.py` and `config.py` in the working directory and next to
the script), set it explicitly: `YT_BOT_ROOT=/path/to/checkout python scripts/e2e_local.py`.
Otherwise the log tail that follows the failure has the bot's own reason.

---

## Clean slate

```bash
docker compose down                     # stop everything
docker compose up -d --build            # rebuild the image and start fresh

# inside the container, only scratch data lives here:
rm -rf /tmp/ytbot-downloads/job-*
```

There is no database, no queue file and no state outside `DOWNLOAD_DIR` — restarting the
bot is always safe. If something is genuinely wedged, delete the service and redeploy it
with the same environment variables; nothing is lost.
