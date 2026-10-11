# Security policy

## Reporting a vulnerability

Please **do not** open a public issue. Report privately through GitHub's
"Report a vulnerability" form on the Security tab, or contact the maintainer directly.

Include what you did, what happened, and the impact you believe it has. You can expect
an acknowledgement within a few days.

## What this project handles

| Secret | Where it lives | Exposure if leaked |
|---|---|---|
| **Bot token** (`123456:AA…`) | The Render worker's environment variables | Full control of the bot: it can read updates and send messages to any chat the bot is in |
| **Worker secret** (`WORKER_SECRET`) | The bot's Telegram database, set via `/setup` | Ability to queue downloads on your worker (resource abuse) |
| **Serverless CLI token** (`app<id>:…`) | `.tgcloud/credentials` on your machine, git-ignored | Ability to deploy code to your bot's Serverless project |

## Rules the code follows

* Nothing secret is committed. `tests/structure.test.mjs` fails the build if
  `tgcloud/lib/config.js` gains a credential.
* `/setup` deletes your message immediately, so the secret does not linger in the chat.
  If the deletion fails (for example in a group), the bot says so and you should delete
  it manually.
* `/config` only ever prints a masked secret (`abcd…wxyz`).
* The worker's `/health` and `/` endpoints expose no credentials: only versions,
  capability flags and queue counters.
* Logs are produced by a redacting logger; fields whose names look like
  `token`/`secret`/`key`/`authorization` are replaced with `[redacted]`.
* `/download`, `/jobs/:id/cancel` and `/stats` require the shared secret, compared with
  `timingSafeEqual` over equal-length buffers.
* URLs are validated against a host allowlist on **both** sides, and must reference a
  single video, so a malformed or lookalike link cannot reach the downloader.
* Request bodies are capped at 16 kB; unknown routes and methods return JSON errors.

## Hardening advice for a public bot

1. Set `OWNER_USER_ID` in `tgcloud/lib/config.js` to your numeric Telegram id **before**
   you announce the bot, so nobody else can claim it with `/setup`. (With the default
   `0`, the first person to send `/setup` becomes the owner.)
2. Lower `MAX_JOBS_PER_DAY` and raise `COOLDOWN_SECONDS` in the same file.
3. Rotate `WORKER_SECRET` from time to time:

   ```bash
   openssl rand -hex 32          # new secret
   # Render dashboard → Environment → WORKER_SECRET → save (redeploys)
   # in Telegram: /setup https://your-worker.onrender.com <new secret>
   ```
4. Rotate the bot token in @BotFather if it may have been exposed, then update
   `BOT_TOKEN` on Render and redeploy.
5. Never paste a token into an issue, a screenshot, a log excerpt or a public chat.
6. Keep the worker on HTTPS (Render provides this). `http://` worker URLs are only
   accepted for `localhost`.

## Out of scope

* Content policy: this bot must only be used for media you own or are authorised to
  save. Reports about it downloading a particular video are not security issues.
* Third-party components (`typetube`, `yt-dlp`, FFmpeg, Telegram's platform) — report
  those upstream, though a note here is welcome if it affects this project.
