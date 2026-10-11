# Deployment guide — Telegram Serverless + TypeTube worker

This repository supports the official **Telegram Serverless** runtime for bot update handlers. Media downloading is performed by a small separate worker because the TypeTube SDK needs Node.js TCP sockets, filesystem access, and FFmpeg. Telegram Serverless itself provides a V8 runtime with the Telegram SDK and outbound HTTP, but no npm packages or filesystem access.

- Telegram Serverless docs: https://core.telegram.org/bots/serverless
- TypeTube package source: https://github.com/botstelegram7-cmyk/typetube
- Bot repository: https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-

**Download priority:** TypeTube first; `yt-dlp` is used only when TypeTube fails. Do not run `yt-dlp` as the primary downloader.

## Architecture and requirements

1. Telegram Serverless receives messages and handles `/start`, `/help`, `/quality`, and YouTube links.
2. The handler sends an authenticated HTTP request to the worker.
3. The worker resolves/downloads with TypeTube, falls back to yt-dlp only on failure, merges audio/video using FFmpeg, and uploads the video to Telegram.
4. The worker removes temporary files after each job.

You need Node.js 18+ and npm on the terminal device. You also need a Telegram bot with Serverless enabled in @BotFather and a place to run the download worker (a Node/Docker host such as a VPS or a container-capable service). The worker is not hosted by Telegram Serverless.

## Part A — Clone the repository

### Linux, macOS, or Termux

```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
node --version
npm --version
```

Use Node.js 18 or newer. On Termux, install it with:

```bash
pkg update
pkg install git nodejs-lts
```

On Windows, install the current Node.js LTS release from https://nodejs.org/, then open CMD or PowerShell and run the same `git clone` and `cd` commands.

## Part B — Deploy the TypeTube worker

Deploy this first so the Serverless handler has a real worker URL to call.

### Option 1: Docker on a VPS

Install Docker on your VPS, clone the repository, then from the repository root run:

```bash
docker build -t youtube-downloader-worker -f worker/Dockerfile worker
```

Create a long random shared secret. On Linux/macOS/Termux, generate one with:

```bash
openssl rand -hex 32
```

Run the worker, replacing the placeholders. Do not paste your actual bot token or shared secret into a public chat or commit them to Git.

```bash
docker run -d --name typetube-worker --restart unless-stopped \
  -p 8080:8080 \
  -e BOT_TOKEN='YOUR_BOTFATHER_BOT_TOKEN' \
  -e WORKER_SECRET='PASTE_YOUR_RANDOM_SECRET' \
  -e PORT=8080 \
  -e MAX_CONCURRENT_DOWNLOADS=2 \
  youtube-downloader-worker
```

Check it from the VPS:

```bash
curl http://127.0.0.1:8080/health
docker logs -f typetube-worker
```

The health endpoint should return JSON with `"ok":true`. For a hosted service, deploy `worker/Dockerfile` with `worker/` as the build context and set the same environment variables in the host dashboard. Copy the service's public HTTPS URL; do not append `/download` to the base URL.

**Network/security:** the worker endpoint is protected by `WORKER_SECRET`. Put it behind HTTPS. Do not expose the shared secret in a URL, command output, screenshot, Git commit, or public issue.

### Worker environment variables

| Variable | Required | Meaning |
|---|---|---|
| `BOT_TOKEN` | Yes | The bot's normal BotFather token, used by the worker to upload media |
| `WORKER_SECRET` | Yes | Long random secret shared with the Serverless handler |
| `PORT` | No | Listening port; defaults to 8080 |
| `MAX_CONCURRENT_DOWNLOADS` | No | Maximum active jobs; defaults to 2 |

TypeTube's public cluster has per-IP request quotas that may change. The worker uses yt-dlp only as a fallback if TypeTube fails. FFmpeg is included in the worker Docker image.

## Part C — Enable Telegram Serverless

1. Open Telegram and chat with **@BotFather**.
2. Select your bot.
3. Open **Serverless** and enable it.
4. Open **CLI Access** and copy the **CLI access token**. This is different from the bot token.
5. Keep both tokens private.

Official walkthrough: https://core.telegram.org/bots/serverless

## Part D — Configure the worker connection locally

From the repository root:

```bash
cp tgcloud/lib/config.example.js tgcloud/lib/config.js
```

On Windows CMD:

```cmd
copy tgcloud\lib\config.example.js tgcloud\lib\config.js
notepad tgcloud\lib\config.js
```

Edit `tgcloud/lib/config.js` and set the worker base URL and the exact same `WORKER_SECRET` configured on the worker host:

```js
export const WORKER_URL = 'https://YOUR-WORKER-HOST.example.com';
export const WORKER_SECRET = 'YOUR_SAME_RANDOM_SECRET';
```

Use the actual HTTPS URL from your worker host. The URL must be the base URL only; the handler appends `/download`. The local `config.js` is git-ignored to help prevent accidental secret commits. Do not remove that ignore rule.

## Part E — Install the CLI and link your bot

From the repository root:

```bash
npm install
npx tgcloud login
```

When prompted, paste the **CLI access token** from **@BotFather → your bot → Serverless → CLI Access → Access token**. Do not paste the regular BotFather bot token here.

Check the local project state:

```bash
npx tgcloud status
npx tgcloud diff
```

## Part F — Test and deploy

First test the message handler using the CLI:

```bash
npx tgcloud run handlers/message '{ chat: { id: 123456, type: "private" }, text: "/start" }'
```

Use a test chat ID for local handler testing; the command may attempt to send a real Telegram message if that chat exists and is accessible to the bot.

Check that the local secret file exists before pushing:

- Linux/macOS/Termux: `test -f tgcloud/lib/config.js && echo "worker config present"`
- Windows CMD: `if exist tgcloud\lib\config.js (echo worker config present) else (echo MISSING worker config)`

Then deploy:

```bash
npx tgcloud push
npx tgcloud migrate
npx tgcloud status
npx tgcloud webhook
```

The first `npx tgcloud migrate` creates the `user_preferences` table used to persist `/quality` settings. Review and confirm the migration when prompted.

The Serverless platform manages the webhook for you. Do **not** run `setWebhook` manually for this Serverless deployment, and do not run the old Python polling bot for the same bot at the same time.

Open the bot in Telegram and test:

1. Send `/start`.
2. Send `/help`.
3. Send `/download 360p https://youtu.be/VIDEO_ID` with a real video URL you are authorized to save.
4. Watch worker logs if the bot reports that it cannot contact the worker.

For day-to-day development:

```bash
npx tgcloud status
npx tgcloud diff
npx tgcloud push
npx tgcloud webhook
```

To pull deployed changes before editing on another device:

```bash
npx tgcloud pull
```

## CMD / PowerShell quick path

```cmd
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
npm install
copy tgcloud\lib\config.example.js tgcloud\lib\config.js
notepad tgcloud\lib\config.js
npx tgcloud login
npx tgcloud status
npx tgcloud run handlers/message "{ chat: { id: 123456, type: 'private' }, text: '/start' }"
npx tgcloud push
npx tgcloud webhook
```

Before `npx tgcloud push`, make sure you have already deployed the worker and filled `tgcloud/lib/config.js` with its URL and shared secret. The CMD block assumes Git and Node.js are installed.

## Termux quick path (Android)

```bash
pkg update && pkg upgrade -y
pkg install -y git nodejs-lts
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
npm install
cp tgcloud/lib/config.example.js tgcloud/lib/config.js
nano tgcloud/lib/config.js
npx tgcloud login
npx tgcloud status
npx tgcloud push
npx tgcloud webhook
```

The Telegram Serverless handlers run on Telegram after deployment; Termux is only the editing/deployment terminal. The download worker itself must run on a compatible Node/Docker host, not in the Telegram Serverless V8 runtime.

## Telegram Bot API upload limit

The hosted Bot API currently allows bot uploads up to 50 MB per file. The worker detects files above that limit and asks the user to retry with a lower quality, for example:

```
/download 480p https://youtu.be/VIDEO_ID
/download 360p https://youtu.be/VIDEO_ID
```

This is a Telegram API limit, not the removed `MAX_FILE_MB` application setting.

## Troubleshooting

- **CLI token rejected:** make sure you used the Serverless CLI access token, not the bot token.
- **No handler runs:** check that `tgcloud/handlers/message.js` exists and run `npx tgcloud webhook`.
- **Worker not configured:** copy `config.example.js` to `config.js`, fill both values, then run `npx tgcloud push` again.
- **Worker HTTP 401:** `WORKER_SECRET` values do not match.
- **Worker HTTP 503:** the worker reached its concurrency limit; retry shortly.
- **TypeTube fails:** worker logs should show that it is using yt-dlp as fallback.
- **Fallback fails:** ensure the worker Docker image has internet access and FFmpeg/yt-dlp installed; check logs.
- **File too large:** retry at a lower quality. The hosted Bot API limit is 50 MB.
- **Do not run both runtimes:** disable the previous polling deployment when switching this bot to Serverless.

## Security checklist

- Never commit `.env`, `.tgcloud/credentials`, `tgcloud/lib/config.js`, bot tokens, or worker secrets.
- Keep the worker behind HTTPS and protect its endpoint with the shared secret.
- Rotate a token/secret immediately if it is exposed.
- Download only content you own or have permission to save; respect copyright and platform terms.
