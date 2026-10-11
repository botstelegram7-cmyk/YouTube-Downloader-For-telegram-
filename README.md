# YouTube Downloader for Telegram

Production-oriented Python Telegram bot using **python-telegram-bot (PTB) 22.8**, yt-dlp, FFmpeg, and environment-based configuration.

> Use only to download content you own or are authorized to save. Respect copyright, YouTube's Terms of Service, and applicable law. This project does not bypass DRM, paywalls, or access restrictions.

## Features
- PTB 22.8 async application with /start, /help, and /quality
- YouTube video and Shorts links; selectable resolution from 360p to 2160p
- Temporary download directories, timeout handling, upload-size checks, logs, and friendly errors
- HTTP health endpoint at /health for Render health checks
- config.py, .env.example, Docker support, Render Blueprint, and deployment guide
- Process environment variables override local .env values

## Requirements
- Python 3.12+
- FFmpeg installed and on PATH
- Telegram bot token from https://t.me/BotFather
- Internet access to Telegram and YouTube

## Environment variables
| Variable | Required | Default | Purpose |
|---|---|---|---|
| BOT_TOKEN | Yes | — | Telegram bot token |
| MAX_FILE_MB | No | 48 | Application upload-size cap |
| DEFAULT_QUALITY | No | 720p | Default video quality |
| PORT | No | 10000 | Health server port |
| DOWNLOAD_TIMEOUT | No | 900 | Download timeout in seconds |
| LOG_LEVEL | No | INFO | Logging level |

MAX_FILE_MB is only the application's cap. Actual upload limits depend on Telegram, hosting plan, and network conditions.

## Local install (Linux/macOS/VPS)
```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
sudo apt-get update && sudo apt-get install -y python3 python3-venv ffmpeg git
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
```
Edit .env and set your real BOT_TOKEN, then run `python bot.py`. The local .env is loaded by python-dotenv; real process environment variables take precedence. Never commit your real .env.

## VPS environment variables
```bash
export BOT_TOKEN='PASTE_YOUR_TOKEN_HERE'
export MAX_FILE_MB=48
export DEFAULT_QUALITY=720p
export PORT=10000
export DOWNLOAD_TIMEOUT=900
python bot.py
```
For production, use systemd or Docker so the bot survives SSH disconnects. Store secrets in a protected environment file (permissions 600) or the host's secret manager, not in Git.

## Docker
```bash
docker build -t youtube-telegram-bot .
docker run -d --name youtube-telegram-bot --restart unless-stopped --env-file .env -p 10000:10000 youtube-telegram-bot
docker logs -f youtube-telegram-bot
```
Health check: http://YOUR_SERVER_IP:10000/health. Expose the port only if required; use a firewall/reverse proxy where appropriate.

## Render Web Service (dashboard)
1. Open https://dashboard.render.com/ and choose **New → Web Service**.
2. Connect this GitHub repository and select the `main` branch.
3. Select Docker runtime so Render builds the included Dockerfile.
4. Under **Environment**, add BOT_TOKEN (from BotFather), MAX_FILE_MB=48, DEFAULT_QUALITY=720p, DOWNLOAD_TIMEOUT=900, and LOG_LEVEL=INFO.
5. Deploy, inspect logs, and check `/health` on the service URL.

Render injects environment variables into the running process; you do not need to upload a real .env file. After changing variables, save and redeploy. Official docs: https://render.com/docs/configure-environment-variables and https://render.com/docs/web-services.

## Render CLI (CMD / terminal / Termux)
Install Render CLI using the official OS-specific instructions at https://render.com/docs/cli, then authenticate with:
```bash
render login
render workspace
render services create --help
```
Use the current help output because Render CLI flags may change. Create a `web_service` using Docker, this repository URL, and the main branch. Set BOT_TOKEN through the Render dashboard rather than putting it in command history. Use `render deploys --help` to find the deploy command supported by your installed CLI.

On Ubuntu/Debian, install basic tools with `sudo apt-get update && sudo apt-get install -y git curl`.

On Android Termux:
```bash
pkg update && pkg upgrade -y
pkg install -y git python clang rust binutils ffmpeg
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
python -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
pip install -r requirements.txt
cp .env.example .env
nano .env
python bot.py
```
Termux is suitable for development/testing but is not a reliable always-on production host. For Render CLI on Android, follow Render's current support guidance; if its CLI binary is not available for Termux, use the dashboard or a supported Linux PC/VPS.

## Serverless note
This bot uses Telegram long polling, which requires a continuously running process. A generic short-lived serverless function may stop and break polling. Use Render Web Service, Docker on a VPS, or another always-on worker. A webhook-based serverless architecture needs an HTTPS webhook handler and is not implemented here.

## Troubleshooting
- BOT_TOKEN missing: set BOT_TOKEN in the process environment or local .env.
- FFmpeg missing: use the Dockerfile or install FFmpeg on your VPS.
- File too large: use `/quality 480p` or reduce MAX_FILE_MB.
- Video unavailable: check URL and access permissions; update yt-dlp after testing.
- Bot stops after SSH disconnect: run it under systemd/Docker or use an always-on service.
- Render health check fails: inspect logs and confirm the PORT is available.

## Security
- Never commit .env, a Telegram token, or credentials.
- Rotate the BotFather token immediately if exposed.
- Review dependency updates and logs before production deployment.
- Download only content you are authorized to save.

## License
MIT. See LICENSE.
