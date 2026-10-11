# Bot profile copy

Telegram has no separate keywords field for ordinary bots, so put the useful words in the
description and about text. These are set in @BotFather — a file in a repository cannot
change your live bot.

## In @BotFather

```text
/setname        → YouTube Downloader

/setdescription → Download YouTube videos and Shorts straight into Telegram. Choose a
                  quality from 360p to 2160p, or grab the audio track only. Powered by
                  TypeTube, with an automatic fallback so a link almost always works.
                  Use it only for content you own or are allowed to save.

/setabouttext   → 🎬 YouTube video & Shorts downloader · 360p–2160p · TypeTube powered

/setuserpic     → a simple camera/download icon you have the rights to use
```

## Command menu

Paste this into `/setcommands`:

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

Owner commands (`/setup`, `/config`, `/stats`, `/unset`) are deliberately not in the
menu, so ordinary users never see them — the bot still answers them with "only available
to the bot owner".

## Keywords to include naturally

YouTube downloader, video downloader, Shorts downloader, Telegram bot, TypeTube,
360p, 480p, 720p, 1080p, 1440p, 2160p, audio only, M4A.

Do not keyword-stuff: Telegram's directories and users both dislike it.

## Which token is which

* **Bot token** (`123456789:AA…`) — from BotFather. The **worker** needs it to upload
  files and edit status messages. Never give it to a third party.
* **Serverless CLI access token** (`app<id>:<secret>`) — from
  BotFather → your bot → **Serverless → CLI Access**. Only used by `npx tgcloud login`.
  Never put it in `BOT_TOKEN`.
* **API ID / API hash** — MTProto application credentials from my.telegram.org. This bot
  does not use them, and they do not raise the Bot API's 50 MB upload limit.
