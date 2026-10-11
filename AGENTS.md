# Repository guide for coding agents

## Project layout

- `tgcloud/` contains only Telegram Serverless modules. Every `.js` file here is treated as a deployable module.
- `tgcloud/handlers/message.js` handles incoming text messages and bot commands.
- `tgcloud/schema.js` declares the Serverless database schema; schema changes require `npx tgcloud migrate` after deploy.
- `tgcloud/lib/` contains shared Serverless modules imported by handlers.
- `worker/` is a separate Node.js service that downloads media using TypeTube first and yt-dlp only as a fallback.
- `bot.py` is the legacy polling implementation; do not run it alongside the Serverless handler for the same bot.
- `docs/` contains architecture and development notes; `deployment.md` is the deployment runbook.

## Runtime boundaries

- Serverless modules can import only `sdk`, its submodules, and other modules under `tgcloud/`. Do not import npm packages or files outside `tgcloud/` from Serverless code.
- Do not put templates such as `config.example.js` inside `tgcloud/`: every `.js` file there is considered a module and module names must be valid.
- `tgcloud/lib/config.js` contains safe empty defaults in Git. Set deployment-specific worker values locally and never commit real secrets.
- Keep `BOT_TOKEN` in the worker host environment. Configure the same `WORKER_SECRET` on the worker host and in `tgcloud/lib/config.js`; never commit real values to source control.
- TypeTube must remain the primary downloader; yt-dlp is fallback-only.

## Before finishing changes

1. Inspect all changed files and imports.
2. Run `node --check` on Node worker JavaScript files when possible.
3. Verify every relative Serverless import ends in `.js` and resolves inside `tgcloud/`.
4. Update docs when commands, file paths, or deployment behavior change.
5. Never claim a remote deployment or runtime test succeeded unless its output confirms success.
