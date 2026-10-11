# Troubleshooting

Start here: the worker's `/health` endpoint answers most questions in one request.

```bash
export WORKER_URL="https://your-worker.onrender.com"
curl -s "$WORKER_URL/health" | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>console.log(JSON.stringify(JSON.parse(s),null,2)))"
```

Then check the table below.

---

## Bot side (Telegram Serverless)

### The bot does not answer at all

| Check | Command / action |
|---|---|
| Are the handlers deployed? | `npx tgcloud status` — should report no local changes |
| Is the webhook in sync? | `npx tgcloud webhook` |
| Did an old process steal the updates? | Stop any other bot polling the same token. A `getUpdates` poller and a webhook cannot coexist: one of them silently wins |
| Did a deploy fail? | `npx tgcloud push` prints the failing module and reason |

### `/start` works but every download says "not connected to a download worker"

The `/setup` step never reached the database.

```text
/setup https://your-worker.onrender.com YOUR_WORKER_SECRET
/config
```

If `/config` shows `Connected: no` afterwards, the secrets are wrong — `/setup` writes
them unconditionally, so a failure here usually means the message never got through.

### "The worker rejected the shared secret"

* Copy `WORKER_SECRET` from the Render dashboard (**Environment** tab) and run `/setup`
  again — most cases are a trailing space or a half-copied value.
* If you rotate the secret on Render, the bot keeps using the old one until `/setup`
  runs again.
* Check you did not paste the **bot token** as the secret, or the **Serverless CLI
  token** (`app<id>:…`) as `BOT_TOKEN` in Render. The worker logs a specific warning
  for that case at boot.

### "The worker did not answer in time"

Free Render instances sleep after 15 minutes without traffic and need ~50 seconds to
wake. Options:

1. Send the link again in a minute (it always works on the second attempt).
2. Set up the keep-alive cron described in
   [DEPLOYMENT.md → Step 8](DEPLOYMENT.md#step-8--keep-the-free-worker-awake).

### `/quality` saves but downloads ignore it

Quality is sent with each submission, so re-send the link. `/download 480p <link>`
forces one quality for a single link. If the worker is down, the status message says so
and nothing is queued.

### "You already have a download running" but nothing is happening

The worker genuinely thinks a job is running (it holds the chat lock), or a job died
without cleaning up.

1. `/status` — if the worker lists a running job, press **Cancel** on its message or
   run `/cancel`.
2. If the worker lists nothing, wait for the cooldown (20 s) and send the link again.
3. Restarting the Render service clears the worker's in-memory queue.

### "You have reached today's limit"

`MAX_JOBS_PER_DAY` (default 20) is per user per rolling 24 hours. Raise it in
`tgcloud/lib/config.js` and `npx tgcloud push`.

### Migrate warns about removed tables or columns

The platform only drops things that are explicitly marked with
`.deprecated('reason')`. Deleting a declaration from `schema.js` leaves the data in
place; that is intentional and safe.

---

## Worker side (Render)

### Deploy fails immediately

Read the first `error` line in the log. Typical causes:

| Log line | Cause | Fix |
|---|---|---|
| `Invalid worker configuration: BOT_TOKEN is required` | The variable was not set | Render → Environment → add it |
| `BOT_TOKEN does not look like a BotFather bot token` | The CLI token was pasted | Use the token from @BotFather → **API Token** |
| `WORKER_SECRET is required` | Missing | Add it (or let `render.yaml` generate it) |
| `Cannot find module 'typetube'` | `npm ci` ran without the lockfile | Redeploy from this repository; `worker/package-lock.json` is committed |

### `/health` shows `ffmpeg: "missing"`

The container did not install FFmpeg, which means you are not building
`worker/Dockerfile`. In the Render service check **Runtime: Docker** and **Root
Directory: worker**. Without FFmpeg the worker still downloads, but it cannot merge
video+audio, and the caption will say `no audio track`.

### `/health` shows `typeTube.supported: false`

The runtime lacks zlib zstd (Node older than 22.15). Redeploy from this repository,
which pins Node 24. Details: [TYPETUBE_COMPAT.md](TYPETUBE_COMPAT.md).

### Downloads are slow

* 0.1 vCPU on the free plan is the ceiling; the Starter plan gives 0.5 vCPU.
* 2160p streams are large; 720p or 480p are much faster and usually fit Telegram.
* Check the worker log for `TypeTube attempt failed` — a slow yt-dlp fallback is much
  slower than TypeTube.
* `DOWNLOAD_WORKERS=6` can help on paid instances; keep 4 on 512 MB.

### "TypeTube could not deliver this video — using the yt-dlp fallback"

The log line above it has the real reason:

| Reason in the log | Meaning |
|---|---|
| `Decompression failed` | Node too old — see `/health typeTube.supported` |
| `[TypeTube Error …]: …` | The cluster refused the track (rate limit, region, unavailable) |
| `TypeTube resolve timed out` | Cluster slow or blocked from the host's network |
| Public quota exhausted | 60 resolves/min or 1,500/day per IP; on shared hosts, other tenants may have used it — a private TypeTube key raises it |

### "YouTube refused the download for the worker's region (HTTP 403)"

YouTube restricted the track for the worker's region, which is common for VEVO and
licensed music. Hosting the worker in **Singapore** (the TypeTube cluster's region)
works best; Frankfurt/Ohio are decent alternatives, and a private TypeTube key routes
differently again.

### The file never arrives, but the status message says "Uploading…"

| Cause | Check |
|---|---|
| File above the 50 MB bot limit | Telegram's limit, not the worker's (`MAX_UPLOAD_MB=0` by default). The status message explains it and offers retry qualities; a self-hosted bot API server lifts the ceiling to 2 GB |
| Telegram rejected the codec | The worker falls back to `sendDocument`; look for `Upload attempt failed, trying a simpler form` in the logs |
| Upload timed out | Slow phone connection or a stalled request; re-try, and prefer a lower quality |
| Bot token revoked or rotated | `getMe` in the worker log at boot says so explicitly |

### "File is too large" even though MAX_UPLOAD_MB is 0

`MAX_UPLOAD_MB=0` removes the **worker's** limit only. The hosted Bot API rejects bot
uploads above **50 MB** before the file ever reaches the chat, and it is Telegram doing
the rejecting:

| What you see | What it means |
|---|---|
| `sendVideo`/`sendDocument` fails with `Bad Request: file is too big` | Telegram refused it — the worker logged the exact method |
| `/health` shows `"maxUploadMb": "unlimited"` | The worker really is unlimited; the limit is upstream |
| `/config` (owner) shows `telegramApi: hosted` | The hosted Bot API is in use, so 50 MB applies |

Three honest options:

1. **Accept 50 MB and let the worker downgrade** — set `MAX_UPLOAD_MB=50` and
   `AUTO_DOWNGRADE=true`, and the bot re-downloads smaller instead of failing (the retry
   buttons do it by hand).
2. **Ask for a smaller quality up front** — 360p/480p are usually inside 50 MB.
3. **Self-host the bot API server** — run
   [telegram-bot-api](https://github.com/tdlib/telegram-bot-api) with an `api_id`/`api_hash`
   from <https://my.telegram.org>, set `TELEGRAM_API_BASE=http://<host>:8081` and
   `MAX_UPLOAD_MB=2000`. Uploads then reach 2 GB per file. Budget ~1 GB of RAM for that
   server, so a free 512 MB instance cannot host both.

### The worker deployed but downloads arrive without sound

The worker could not find FFmpeg, so it delivered the video track on its own:

| Host / runtime | Why | Fix |
|---|---|---|
| Voroa, **Node.js buildpack** | The buildpack cannot install system packages, so there is no FFmpeg (and no yt-dlp) | Switch the service to the **Docker (Dockerfile)** runtime — the image installs both |
| Render, Docker | The image should have FFmpeg | Check `/health` → `"ffmpeg": "…"`; if it says `missing`, redeploy from this repository |
| Any host, `FFMPEG_BINARY` set | The path does not exist in the container | Unset it, or point it at a real binary |

### The container is killed at random

512 MB is the free-plan ceiling. Reduce `MAX_CONCURRENT_DOWNLOADS` to `1`,
`DOWNLOAD_WORKERS` to `3`, and `CHUNK_SIZE_MB` to `4`, then redeploy. 2160p is the most
likely trigger.

### Nothing works and you want a clean slate

```bash
# Worker
curl -s "$WORKER_URL/health"                       # is it alive?
# Render: Manual Deploy → Deploy latest commit      # rebuild the image

# Bot
npx tgcloud status && npx tgcloud push && npx tgcloud webhook
# then in Telegram:
/setup <worker-url> <secret>
```

---

## Local development

### Tests fail with `Cannot find package 'sdk'`

The Serverless handlers import a module that only exists inside Telegram's runtime. Run
them through the stub loader:

```bash
node --import ./tests/stubs/register.mjs --test tests/     # what `npm test` does
```

### Worker tests fail with `ERR_INVALID_ARG_TYPE ... promisify`

You are on Node older than 22.15. Check:

```bash
node -e "console.log(process.version, typeof require('node:zlib').zstdCompress)"
```

Use Node 22.15+ / 24 (or `nvm use 24`) before running the worker suite.

### `npm install` warns `EBADENGINE`

Expected on older local Node: `worker/package.json` requires Node ≥ 22.15 for TypeTube.
Switch Node versions rather than lowering the requirement.
