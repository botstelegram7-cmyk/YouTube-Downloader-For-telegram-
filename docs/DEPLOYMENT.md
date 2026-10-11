# Deployment guide — Telegram Serverless bot + Render worker, entirely from Termux

This is the complete, phone-only walkthrough. Everything below runs in **Termux on
Android** plus two phone-friendly web dashboards (@BotFather inside Telegram and
render.com). No PC, no VPS, no SSH key required.

Read it once before starting; it takes about 30 minutes the first time.

**What you will end up with**

| Piece | Where it runs | What it does |
|---|---|---|
| Bot handlers + database | **Telegram Serverless** (Telegram's own cloud) | /start, /help, /quality, buttons, quotas, config |
| Download worker | **Render** (free Docker web service) | Resolves and downloads with **TypeTube**, merges with FFmpeg, uploads to Telegram |
| Fallback engine | inside the worker | yt-dlp, used only when TypeTube cannot deliver |

---

## Contents

1. [Step 0 — Termux setup](#step-0--termux-setup)
2. [Step 1 — Create the bot and enable Serverless](#step-1--create-the-bot-and-enable-serverless)
3. [Step 2 — Get the code onto the phone](#step-2--get-the-code-onto-the-phone)
4. [Step 3 — Deploy the worker on Render](#step-3--deploy-the-worker-on-render)
5. [Step 4 — Test the worker](#step-4--test-the-worker)
6. [Step 5 — Connect the bot to the worker](#step-5--connect-the-bot-to-the-worker)
7. [Step 6 — Deploy the bot to Telegram Serverless](#step-6--deploy-the-bot-to-telegram-serverless)
8. [Step 7 — First real download](#step-7--first-real-download)
9. [Step 8 — Keep the free worker awake](#step-8--keep-the-free-worker-awake)
10. [Step 9 — Day-two operations](#step-9--day-two-operations)
11. [Free plan reality check](#free-plan-reality-check)
12. [Troubleshooting](#troubleshooting)
13. [Command cheat sheet](#command-cheat-sheet)

---

## Step 0 — Termux setup

Install **Termux from F-Droid or the Termux GitHub releases**, not the outdated Play
Store build. Then:

```bash
pkg update && pkg upgrade -y
pkg install -y git nodejs-lts openssl-tool curl
termux-wake-lock
```

Check the tools:

```bash
node --version    # v22.x or newer is fine for the CLI
npm --version
git --version
```

> `termux-wake-lock` stops Android from freezing Termux during long commands. Run
> `termux-wake-unlock` when you are done.

Grant Termux access to your files only if you want to keep downloads on the phone:

```bash
termux-setup-storage
```

---

## Step 1 — Create the bot and enable Serverless

Open Telegram and talk to [@BotFather](https://t.me/BotFather):

1. `/newbot` → give it a name and a username ending in `bot`.
2. **Copy the bot token** it prints (`123456789:AA...`). This is the **bot token**; the
   worker needs it to upload files.
3. Open **your bot → Serverless** and **turn Serverless on**. This unlocks handlers,
   the database and the CLI token.
4. Open **your bot → Serverless → CLI Access** and copy the **Access token**. It looks
   like `app12345678:abcdef...`. This is a *different* token from the bot token.

Optional but recommended, still in BotFather:

```text
/setdescription   → Download YouTube videos and Shorts in selectable quality.
/setabouttext     → 🎬 YouTube video & Shorts downloader | 360p–2160p | TypeTube powered
/setcommands      → (paste the block below)
```

```text
start - Start the downloader
help - How to use the bot
quality - Choose the default resolution
audio - Audio-only (M4A) mode
video - Video with sound (default)
download - One-off download at a chosen quality
status - Worker health and your running job
cancel - Stop your running download
about - Credits, limits and the legal note
```

Keep both tokens in a safe note. Never paste them into a public chat or a GitHub file.

---

## Step 2 — Get the code onto the phone

Fork the repository first if you want your own copy (GitHub app or the web UI), then:

```bash
cd ~
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
npm install
```

Run the built-in checks to confirm the checkout is healthy:

```bash
npm run check
```

You should see `Serverless module check passed`, the test suites passing, and no
failures. This also validates the deployable layout *before* it reaches Telegram.

> **Forking tip:** Render deploys from a Git repository. Forking means you control the
> branch Render builds, so your deployment cannot be changed by someone else's commit.

---

## Step 3 — Deploy the worker on Render

The worker is a small always-needed Docker service. The repository ships
[`render.yaml`](../render.yaml), so the fastest path is a **Blueprint**.

### 3a. Create the service

1. Open <https://dashboard.render.com> on the phone and sign in (a payment method is
   required even for the free plan).
2. Tap **New → Blueprint**.
3. Connect **GitHub** and pick your repository (the fork from Step 2).
4. Render reads `render.yaml` and shows one service: `youtube-dl-worker`.
5. It asks for the values it cannot invent:
   * **`BOT_TOKEN`** → paste the bot token from Step 1.
   * **`WORKER_SECRET`** → Render generated a long random value for you. Leave it.
6. Tap **Apply** / **Create**. The first build takes 3–6 minutes (it installs FFmpeg
   and yt-dlp).

<details>
<summary>Prefer to configure it by hand instead of a Blueprint?</summary>

1. **New → Web Service** → connect the repository.
2. **Root Directory**: `worker`  ← this matters, do not leave it blank.
3. **Runtime**: Docker. Render finds `worker/Dockerfile`.
4. **Instance Type**: **Free**.
5. **Region**: **Singapore** — the TypeTube cluster lives there, so downloads are
   faster and fewer music videos are region-blocked.
6. **Health Check Path**: `/health`.
7. Add environment variables:

   | Key | Value |
   |---|---|
   | `BOT_TOKEN` | your bot token |
   | `WORKER_SECRET` | a long random string: `openssl rand -hex 32` |
   | `MAX_CONCURRENT_DOWNLOADS` | `1` |
   | `DOWNLOAD_WORKERS` | `4` |
   | `CHUNK_SIZE_MB` | `8` |
   | `MAX_UPLOAD_MB` | `50` |
   | `YTDLP_FALLBACK` | `true` |
   | `LOG_LEVEL` | `info` |

8. **Create Web Service**.

</details>

### 3b. Collect the two values the bot needs

On the service page:

* **URL** — the top of the page shows something like
  `https://youtube-dl-worker.onrender.com`. Copy the base URL, **without** `/download`.
* **Secret** — **Environment** tab → reveal **`WORKER_SECRET`** → copy the whole value.

Write both down; Step 5 needs them.

### 3c. Wait for "Live"

The deploy log ends with `Worker listening`. If the service shows **Deploy failed**,
open the log and search for the first `error` line — the worker prints a readable list
of any configuration problem.

---

## Step 4 — Test the worker

From Termux, with `WORKER_URL` set to your service URL:

```bash
export WORKER_URL="https://youtube-dl-worker.onrender.com"

curl -s "$WORKER_URL/health"
```

Expected (pretty-printed for readability):

```json
{
  "ok": true,
  "service": "yt-telegram-download-worker",
  "version": "2.0.0",
  "runtime": { "node": "v24.x", "ffmpeg": "/usr/bin/ffmpeg" },
  "typeTube": { "supported": true, "zstd": true }
}
```

Three things to check:

* `"ok": true` — the server is up.
* `ffmpeg` is a path, not `"missing"`.
* `typeTube.supported` is `true` — if it is `false`, the image is running an old Node
  version; redeploy from this repository as-is.

Now confirm the secret works (should return `401 unauthorized` without it, `200` with it):

```bash
export WORKER_SECRET="paste-the-secret-here"
curl -s -o /dev/null -w '%{http_code}\n' "$WORKER_URL/stats"                              # 401
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $WORKER_SECRET" "$WORKER_URL/stats"   # 200
```

> **First request is slow.** On the free plan the service sleeps after 15 minutes of
> inactivity, so a cold `curl` can take 30–60 seconds. That is expected; Step 8 fixes it.

---

## Step 5 — Connect the bot to the worker

The bot keeps the worker URL and secret **in its Telegram database**, not in git, so
you configure it by messaging your own bot:

```text
/setup https://youtube-dl-worker.onrender.com YOUR_WORKER_SECRET
```

Send it as one message, with your real values. What happens:

* the bot replies `✅ Worker connected: …`;
* it **deletes your /setup message** so the secret does not stay in the chat history;
* the first person to run `/setup` becomes the owner of the bot.

Verify with:

```text
/config
```

You should see the URL, a masked secret (`abcd…wxyz`) and `Connected: yes`.

> Rotate the secret any time with `/setup` again, or forget the worker with `/unset`.

---

## Step 6 — Deploy the bot to Telegram Serverless

Still in Termux, inside the repository folder:

```bash
npx tgcloud login
```

Paste the **Serverless CLI access token** from Step 1 (`app<id>:<secret>`). It is saved
in `.tgcloud/`, which is git-ignored. If it asks for the bot API token instead, you
pasted the wrong one.

Now inspect and deploy:

```bash
npx tgcloud status     # what differs between your folder and the cloud
npx tgcloud diff       # line-by-line view of those differences
npx tgcloud push       # upload the modules under tgcloud/
npx tgcloud migrate    # create the database tables
npx tgcloud webhook    # confirm the platform-managed webhook
```

`migrate` creates three tables: `user_preferences`, `bot_settings` (where your worker
credentials live) and `download_jobs`.

Optionally dry-run the handler locally before or after pushing:

```bash
npx tgcloud run handlers/message '{ chat: { id: 123456, type: "private" }, from: { id: 123456 }, text: "/start" }'
```

Then message your bot on Telegram:

```text
/start
/help
```

> If `/start` works but downloads say "not connected", the `/setup` from Step 5 did not
> reach the database — re-run it and check `/config`.

---

## Step 7 — First real download

Send your test link:

```text
/config
/download 360p https://youtu.be/jNQXAC9IVRw
```

What you should see:

1. `⏳ Contacting the worker…`
2. `⏳ Starting now` with a **Cancel** button
3. progress edits (`⬇️ Downloading with TypeTube… 42%`)
4. `⬆️ Uploading 3.2 MB to Telegram…`
5. the video itself, with a caption listing quality and size

**Keep the first test small.** 360p keeps you far below Telegram's 50 MB bot-upload
limit, and TypeTube's free tier allows 60 lookups/minute and 1,500/day per IP address.

Now try the normal flow — just sending a link — and the buttons:

```text
/quality          → tap a resolution
/audio            → switch to audio-only
<send a link>     → uses your saved settings
/status           → worker health + your job
/cancel           → stop a running download
```

---

## Step 8 — Keep the free worker awake

Free Render services sleep after **15 minutes** with no incoming request, and waking up
takes ~50 seconds. A cron ping every 10 minutes keeps it warm.

1. Create a free account at <https://cron-job.org> (or use UptimeRobot).
2. **Create cronjob**:
   * URL: `https://youtube-dl-worker.onrender.com/health`
   * Schedule: every **10 minutes**
   * Enable **failure notifications** so you learn when the worker dies.
3. Save. The worker now answers instantly.

Budget check: 24/7 awake ≈ **744 instance-hours/month**, and the free plan allows 750
per workspace. Since this is your only Render service, you stay inside the limit — but
do not add a second free service in the same workspace.

---

## Step 9 — Day-two operations

### Update the bot (handlers / database)

```bash
cd ~/YouTube-Downloader-For-telegram-
git pull
npm install
npm run check
npx tgcloud push
npx tgcloud migrate
```

### Update the worker

Push to the branch Render builds, or tap **Manual Deploy → Deploy latest commit** in
the Render dashboard. The worker must restart for a new image, so a deploy briefly
interrupts downloads (in-flight jobs are cancelled cleanly and their status message
says so).

### Watch logs

* Worker: Render dashboard → your service → **Logs** (one JSON object per line).
* Bot: `npx tgcloud run handlers/message '{ ... }'` prints handler `console` output.

### Change a limit

Edit the environment variables in the Render dashboard (worker side) or
`tgcloud/lib/config.js` and re-push (bot side, e.g. `MAX_JOBS_PER_DAY`).

### Rotate the worker secret

```bash
openssl rand -hex 32
```

1. Render dashboard → Environment → set the new `WORKER_SECRET` → save (it redeploys).
2. In Telegram: `/setup https://your-worker.onrender.com <new secret>`.

---

## Free plan reality check

| Limit | Value | What it means here |
|---|---|---|
| RAM | 512 MB | One download at a time (`MAX_CONCURRENT_DOWNLOADS=1`). TypeTube uses `DOWNLOAD_WORKERS=4` × 8 MB chunks, well inside the budget |
| CPU | 0.1 vCPU (shared) | Muxing is `-c copy`, so it costs almost nothing. Avoid 2160p on the free plan |
| Sleep | after 15 min idle | ~50 s cold start; the keep-alive job in Step 8 removes it |
| Instance hours | 750 / month / workspace | One always-on service ≈ 744 h — fits, but nothing else can share it |
| Outbound bandwidth | 100 GB / month | A 50 MB upload is 50 MB outbound; ~2,000 downloads/month |
| Build minutes | 500 / month | A worker image build is a few minutes |
| PostgreSQL | deleted after 90 days | Not used — the bot's database lives inside Telegram |

**What sleeps are not:** if you skip Step 8, the first link after a quiet period can
fail with "the worker did not answer in time". The bot tells you exactly that, and
re-sending the link a minute later works. This is expected behaviour, not a bug.

---

## Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `/start` works, downloads say "not connected to a download worker" | `/setup` never ran | Send `/setup <url> <secret>` (Step 5) |
| "The worker rejected the shared secret" | Typo, or the secret changed in Render | Copy `WORKER_SECRET` from the Render Environment tab and run `/setup` again |
| "The worker did not answer in time" | Free instance asleep, or a bad URL | Check `curl $WORKER_URL/health`; set up Step 8 |
| `curl` shows `typeTube.supported: false` | Old Node in the image | Redeploy from this repository (it pins Node 24) |
| "file is too large / above Telegram's 50 MB limit" | Video bigger than the bot API allows | Use the **Retry 480p/360p** buttons, or `/quality 360p` |
| "YouTube refused the download for the worker's region (403)" | Region-restricted track (common for VEVO/music) | Host the worker in Singapore, or try another video |
| Downloads work but slowly | 0.1 vCPU free instance | Upgrade to Starter ($7/mo) for a dedicated 0.5 vCPU |
| `npx tgcloud push` says the module names are invalid | A file under `tgcloud/` has a non-identifier name | `npm run check:modules` names the offending file |
| `/quality` saves but downloads ignore it | The worker reads the quality from the submission, not the DB | Re-send the link; `/download 480p <url>` forces it once |
| Bot answers twice | An old polling bot is still running | Stop any Python/other process polling the same token; Serverless owns the webhook |
| Migrate warns about dropped columns | A column was removed from `schema.js` | Drops require `.deprecated('reason')`; restore or accept the drop |

More detail: [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

---

## Command cheat sheet

```bash
# --- Termux, one-time -------------------------------------------------------
pkg install -y git nodejs-lts openssl-tool curl
termux-wake-lock
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
npm install && npm run check

# --- Bot (Telegram Serverless) ---------------------------------------------
npx tgcloud login                    # paste the CLI access token (app<id>:<secret>)
npx tgcloud status                   # local vs cloud
npx tgcloud diff                     # what exactly changed
npx tgcloud push                     # deploy tgcloud/ modules
npx tgcloud migrate                  # apply schema changes
npx tgcloud webhook                  # inspect the managed webhook
npx tgcloud run handlers/message '{ chat: { id: 1, type: "private" }, from: { id: 1 }, text: "/start" }'

# --- Worker (Render) --------------------------------------------------------
curl -s "$WORKER_URL/health" | node -e "let s='';process.stdin.on('data',d=>s+=d).on('end',()=>console.log(JSON.stringify(JSON.parse(s),null,2)))"
curl -s -H "Authorization: Bearer $WORKER_SECRET" "$WORKER_URL/stats"

# --- Local pre-flight before every push -------------------------------------
npm run check            # modules + syntax + all tests
npm run test:all         # both suites only
openssl rand -hex 32     # new shared secret
```

Operator commands inside Telegram:

```text
/setup <worker-url> <secret>   # connect the worker (owner)
/config                        # show the configuration (owner)
/stats                         # worker queue, memory, TypeTube status (owner)
/unset                         # forget the credentials (owner)
/status                        # worker health + your running job
/cancel                        # stop your download
```
