# TypeTube download worker

This service runs outside Telegram Serverless because TypeTube needs Node.js sockets, a filesystem, and FFmpeg. TypeTube is the primary downloader; yt-dlp is used only if TypeTube fails.

## Required environment

- `BOT_TOKEN`: the Telegram bot token from BotFather
- `WORKER_SECRET`: long random shared secret, matching the local-only `tgcloud/lib/config.js`
- `PORT`: supplied by the host (defaults to 8080)
- `MAX_CONCURRENT_DOWNLOADS`: optional worker concurrency (default 2)

## Run with Docker

From the repository root:

```bash
docker build -t youtube-downloader-worker -f worker/Dockerfile worker
docker run --rm -p 8080:8080 \
  -e BOT_TOKEN='YOUR_BOT_TOKEN' \
  -e WORKER_SECRET='YOUR_LONG_RANDOM_SECRET' \
  youtube-downloader-worker
```

Check `http://localhost:8080/health`. Never commit real tokens or shared secrets.

The worker sends videos through Telegram's hosted Bot API, which currently limits uploads to 50 MB per file. TypeTube's public cluster has per-IP quotas; those quotas can change. Use only for content you are authorized to download.
