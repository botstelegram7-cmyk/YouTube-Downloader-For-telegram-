# Telegram bot profile

Telegram does not provide a separate public "tags" field for ordinary bots. Put relevant search keywords in the bot's description/about text, and set the command menu with @BotFather.

## Suggested description

Download YouTube videos and Shorts in selectable quality. TypeTube-powered downloads with automatic yt-dlp fallback and delivery in Telegram. Choose 360p–2160p. Use only for content you own or are authorized to save.

## Suggested short about text

🎬 YouTube video & Shorts downloader | Selectable quality | TypeTube + fallback

## Keywords / tags to include naturally

YouTube downloader, video downloader, Shorts downloader, Telegram bot, TypeTube, 360p, 480p, 720p, 1080p, 1440p, 2160p.

## Set these in @BotFather

1. Open @BotFather and select your bot.
2. Use `/setdescription` and paste the suggested description.
3. Use `/setabouttext` and paste the short about text.
4. Use `/setcommands` and paste:

```text
start - Start the downloader
help - Show help and usage
quality - Set your default download quality
download - Download a YouTube URL at a chosen quality
```

These profile fields are set through @BotFather; committing this file documents the suggested text but does not automatically change the live Telegram profile.

## Important: credentials are different

- **Serverless CLI access token** (`app<id>:<secret>`): use only with `npx tgcloud login`.
- **BotFather bot token**: required by the separate worker's Bot API status-message calls and upload workflow. Do not paste the CLI access token into `BOT_TOKEN`.
- **API ID / API hash**: MTProto application credentials from `my.telegram.org`; they do not replace either token and do not automatically increase Telegram Serverless/Bot API upload limits.

The current worker uses the hosted Bot API for file uploads, so the 50 MB per-file limit still applies. Supporting larger uploads requires a deliberate change to the upload transport and must be tested separately from Telegram Serverless. See the official Bot API limits: https://core.telegram.org/bots/faq#how-do-i-download-files and https://core.telegram.org/bots/api#sendvideo
