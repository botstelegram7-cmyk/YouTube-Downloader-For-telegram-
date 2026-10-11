# Architecture

## Runtime split

```text
Telegram update
      |
      v
Telegram Serverless
  tgcloud/handlers/message.js
  - /start and /help
  - quality preferences (database)
  - validates YouTube URLs
      |
      | authenticated HTTPS POST /download
      v
Node/Docker worker (worker/)
  - TypeTube first
  - yt-dlp only if TypeTube fails
  - FFmpeg muxing
  - Bot API upload (hosted Bot API file limit applies)
  - deletes temporary files
```

## Source tree rules

- `tgcloud/` is the Serverless module boundary. Only JavaScript modules under its supported paths are deployed.
- Keep example configs, documentation, Docker files, and package manifests outside `tgcloud/` so they cannot accidentally be interpreted as deployable module names.
- `tgcloud/lib/config.js` is a valid shared module with empty defaults. Fill it locally when connecting a worker; the same `WORKER_SECRET` must also be set in the worker host environment. Never commit real secrets.
- `worker/` is a standalone Node.js service and has its own `package.json` and Dockerfile.
- `bot.py` is retained as a legacy alternative and is not the Serverless entry point.

## Deployment order

1. Deploy the worker and configure `BOT_TOKEN` and `WORKER_SECRET` on the worker host.
2. Set the worker base HTTPS URL and matching shared secret in `tgcloud/lib/config.js` locally.
3. Run `npx tgcloud status`, then `npx tgcloud push`.
4. Run `npx tgcloud migrate` when the database schema has pending changes.
5. Run `npx tgcloud webhook` and test `/start`, `/help`, then a valid URL.

`/start` and `/help` do not depend on the worker being configured. Download requests should return a clear setup message while the worker settings are empty.
