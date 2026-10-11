# YouTube Downloader for Telegram

Telegram Serverless bot handlers plus a separate TypeTube download worker.

## Recommended deployment

Use [`deployment.md`](deployment.md) for the step-by-step terminal guide for Telegram Serverless, Windows CMD/PowerShell, Linux/macOS, and Android Termux.

**Architecture:** Telegram Serverless handles commands and updates. The worker uses **TypeTube first** and **yt-dlp only as a fallback**, with FFmpeg for audio/video muxing. TypeTube needs Node.js TCP sockets and filesystem access, which are not available in Telegram Serverless's V8 runtime, so the worker must run on a compatible Node/Docker host.

## Features

- Telegram Serverless handlers: `/start`, `/help`, `/quality`, YouTube URLs, and `/download <quality> <URL>`
- TypeTube-first media download; yt-dlp fallback only after TypeTube errors
- Authenticated worker requests with a shared secret
- Temporary files cleaned after each download
- Telegram hosted Bot API 50 MB upload-limit detection
- Existing Python polling bot retained as a legacy alternative in `bot.py`

## Quick deployment summary

1. Enable Serverless in @BotFather → your bot → **Serverless**.
2. Deploy the Node/Docker download worker described in [`worker/README.md`](worker/README.md).
3. Clone this repository and run `npm install`.
4. Copy `tgcloud/lib/config.example.js` to `tgcloud/lib/config.js` and fill in the worker HTTPS URL and the same shared secret used by the worker.
5. Run `npx tgcloud login` and paste the **Serverless CLI access token** from @BotFather → Serverless → CLI Access.
6. Run `npx tgcloud run handlers/message '{ chat: { id: 123456, type: "private" }, text: "/start" }'`.
7. Run `npx tgcloud push`, then `npx tgcloud migrate` and confirm the `user_preferences` table migration.
8. Run `npx tgcloud webhook`.
9. Test with `/download 360p https://youtu.be/VIDEO_ID`.


Never commit tokens or the local `tgcloud/lib/config.js` file. See [`deployment.md`](deployment.md) for all commands and troubleshooting.

## Legacy polling deployment

The original Python bot in `bot.py` is retained for users who want to run a conventional always-on process instead. It is **not** the Telegram Serverless entry point. See the Python requirements, Dockerfile, and Render configuration if you intentionally want that alternative. Do not run both polling and Serverless handlers for the same bot at the same time.

## Safety and upload limits

Only download media you own or are authorized to save. Respect copyright, YouTube's terms, and applicable law. The hosted Telegram Bot API currently limits bot uploads to 50 MB per file; the worker reports oversized downloads and recommends a lower quality.
