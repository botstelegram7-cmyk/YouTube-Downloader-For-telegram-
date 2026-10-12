# Bot profile copy

Telegram has no keywords field for ordinary bots, so the words that matter go in the
description and about text. These are set in [@BotFather](https://t.me/BotFather) — a file
in a repository cannot change your live bot.

## In @BotFather

```text
/setname        → YouTube Downloader

/setdescription → Send a video link and get a playable file back. Pick the quality from
                  buttons (144p up to whatever the source really offers), or take the
                  audio track only. Videos arrive streaming-ready with a thumbnail and
                  title, not as anonymous documents. Use it only for content you own or
                  are allowed to save.

/setabouttext   → 🎬 Video & audio downloader for Telegram · yt-dlp + FFmpeg · choose your quality

/setuserpic     → a simple download/play icon you have the rights to use
```

## Command menu

Paste this into `/setcommands` — it matches what the bot publishes at startup, so the
menu button and the bot itself always agree:

```text
start - Show the welcome screen and feature list
help - How to use the bot, step by step
status - What is running now, plus limits and versions
cancel - Stop your download, conversion and upload
limits - Why files are capped and how to raise the cap
id - Show your chat id (for STARTUP_NOTIFY_CHAT_ID)
about - Version, engine and credits
```

## Keywords to include naturally

video downloader, Telegram bot, download video to Telegram, choose quality, 144p, 240p,
360p, 480p, 720p, 1080p, audio only, M4A, playable video, yt-dlp, FFmpeg.

Do not keyword-stuff: Telegram's directories and users both dislike it.

## Which credential is which

| Credential | Format | What it is for |
|---|---|---|
| **Bot token** | `123456789:AA…` from BotFather | The bot's only mandatory setting (`BOT_TOKEN`). It authenticates every API call. Treat it as a password; `/revoke` issues a new one. |
| **Your user id** | a number, e.g. `987654321` | `OWNER_USER_ID` locks the bot to you; `STARTUP_NOTIFY_CHAT_ID` receives the startup introduction. Send `/id` to the bot to learn it. |
| **Webhook secret** | any string, e.g. 32 hex chars | `WEBHOOK_SECRET` — Telegram echoes it back in `X-Telegram-Bot-Api-Secret-Token`; requests without it get a 403. `render.yaml` generates one for you. |
| **api_id / api_hash** | from [my.telegram.org](https://my.telegram.org) | **A phone-based login for your own account**, not the bot. Only needed if you run the self-hosted Bot API server that lifts uploads from 50 MB to 2000 MB (see [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#step-4--uploads-larger-than-50-mb-the-local-bot-api-server)). They are as sensitive as the bot token; never commit them. |

Environment variables for all of these are documented in `.env.example`.

## Limits to mention if you write your own copy

* Uploads: **50 MB** through Telegram's cloud API, **2000 MB** with a self-hosted Bot API
  server. That is Telegram's ceiling, not this bot's choice — `/limits` explains it to users.
* One request per user at a time; a new link replaces the running one, `/cancel` stops it.
* No DRM bypass, no login-gated content unless you supply your own `COOKIES_FILE`.
