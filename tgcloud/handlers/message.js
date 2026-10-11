import { api, db, fetch } from 'sdk';
import { eq } from 'sdk/db';
import { userPreferences } from '../schema.js';
import { WORKER_URL, WORKER_SECRET } from '../lib/config.js';
import { normalizeYouTubeUrl, parseCommand, QUALITIES } from '../lib/youtube.js';

export default async function (message) {
  if (!message?.chat?.id || typeof message.text !== 'string') return;

  const chatId = message.chat.id;
  const text = message.text.trim();
  const { command, args } = parseCommand(text);

  if (command === '/start') {
    await api.sendMessage({
      chat_id: chatId,
      text: '🎬 YouTube Downloader\n\nSend a YouTube video or Shorts URL to download it.\n\n/quality 720p — choose quality\n/help — help\n\nDownloads use TypeTube first and yt-dlp as a fallback. Only download content you are authorized to save.'
    });
    return;
  }

  if (command === '/help') {
    await api.sendMessage({
      chat_id: chatId,
      text: 'Send a YouTube video/Shorts URL.\n\nCommands:\n/quality 360p\n/quality 480p\n/quality 720p\n/quality 1080p\n/quality 1440p\n/quality 2160p\n\nThe hosted Telegram Bot API upload limit is 50 MB per file. Large files may need a lower quality. The downloader worker must be deployed separately; see deployment.md.'
    });
    return;
  }

  if (command === '/quality') {
    const quality = (args[0] || '').toLowerCase();
    if (!QUALITIES.includes(quality)) {
      await api.sendMessage({
        chat_id: chatId,
        text: 'Usage: /quality 720p\nOptions: ' + QUALITIES.join(', ')
      });
      return;
    }

    const userId = message.from?.id;
    if (!userId) {
      await api.sendMessage({ chat_id: chatId, text: 'I could not identify your Telegram user ID.' });
      return;
    }

    await db.insert(userPreferences)
      .values({ userId, quality })
      .onConflictDoUpdate({ target: userPreferences.userId, set: { quality } })
      .run();

    await api.sendMessage({
      chat_id: chatId,
      text: `Default quality saved as ${quality}. Send a YouTube URL to use it, or use /download <quality> <URL> for a one-off choice.`
    });
    return;
  }

  const userId = message.from?.id;
  const preference = userId
    ? await db.select().from(userPreferences).where(eq(userPreferences.userId, userId)).get()
    : null;
  let quality = preference?.quality || '720p';
  let urlText = text;

  if (command === '/download') {
    quality = (args[0] || '').toLowerCase();
    urlText = args.slice(1).join(' ');
    if (!QUALITIES.includes(quality)) {
      await api.sendMessage({
        chat_id: chatId,
        text: 'Usage: /download 720p https://youtu.be/VIDEO_ID'
      });
      return;
    }
  }

  const normalizedUrl = normalizeYouTubeUrl(urlText);
  if (!normalizedUrl) {
    await api.sendMessage({
      chat_id: chatId,
      text: 'Send a valid YouTube video/Shorts URL, or use /download 720p <URL> to choose quality.'
    });
    return;
  }

  if (!WORKER_URL || !WORKER_SECRET) {
    await api.sendMessage({
      chat_id: chatId,
      text: 'The Serverless bot is deployed, but the download worker is not configured yet. Follow the worker setup in deployment.md.'
    });
    return;
  }

  const status = await api.sendMessage({
    chat_id: chatId,
    text: '⏳ Download request accepted. TypeTube will be tried first; yt-dlp is the fallback.'
  });

  try {
    const response = await fetch(`${WORKER_URL.replace(/\/$/, '')}/download`, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Authorization': `Bearer ${WORKER_SECRET}`
      },
      body: JSON.stringify({
        url: normalizedUrl,
        quality,
        chat_id: chatId,
        status_message_id: status.message_id
      })
    });

    if (!response.ok) {
      const detail = (await response.text()).slice(0, 300);
      throw new Error(`worker returned HTTP ${response.status}: ${detail}`);
    }
  } catch (error) {
    console.error('Worker request failed', String(error));
    await api.editMessageText({
      chat_id: chatId,
      message_id: status.message_id,
      text: '❌ Could not contact the download worker. Check its URL, deployment health, and shared secret.'
    });
  }
}
