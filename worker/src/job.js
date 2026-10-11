/**
 * One download job, end to end: status messages → download → upload → cleanup.
 *
 * The user-visible contract:
 *   1. the handler creates the status message and hands us its id;
 *   2. we edit that message while working (throttled, so we never hit Telegram's
 *      edit rate limits even on a slow download);
 *   3. on success the status message is deleted and the media message stands alone;
 *   4. on failure the status message is replaced by an explanation and, when the
 *      file was too large, by buttons that retry at a smaller quality.
 */

import { basename } from 'node:path';
import { CancelledError, Downloader, TooLargeError, formatMb } from './downloader.js';
import { TelegramApiError } from './telegram.js';
import { createJobDir, removeJobDir } from './workspace.js';
import { fetchThumbnail } from './thumbnail.js';
import { QUALITIES, heightOf, nextLowerQuality } from './validate.js';

const CHAT_ACTION_INTERVAL_MS = 4_000;

class StatusUpdater {
  constructor({ telegram, chatId, messageId, log, minIntervalMs = 6_000 }) {
    this.telegram = telegram;
    this.chatId = chatId;
    this.messageId = messageId;
    this.log = log;
    this.minIntervalMs = minIntervalMs;
    this.lastText = null;
    this.lastSentAt = 0;
  }

  /** Throttled edit: at most one update per `minIntervalMs`, always the newest text. */
  async update(text, { force = false } = {}) {
    if (!text || text === this.lastText) return false;
    const now = Date.now();
    if (!force && now - this.lastSentAt < this.minIntervalMs) return false;

    this.lastText = text;
    this.lastSentAt = now;
    try {
      await this.telegram.editStatus(this.chatId, this.messageId, text);
      return true;
    } catch (error) {
      this.log?.debug('Status update failed', { message: error.message });
      return false;
    }
  }

  /**
   * Final edit: always sent, with an optional inline keyboard and any previous
   * keyboard removed when none is given.
   */
  async final(text, keyboard) {
    this.lastText = text;
    try {
      await this.telegram.call('editMessageText', {
        chat_id: this.chatId,
        message_id: this.messageId,
        text,
        reply_markup: keyboard ?? { inline_keyboard: [] },
        link_preview_options: { is_disabled: true }
      });
      return true;
    } catch (error) {
      if (error instanceof TelegramApiError && (error.isNotModified || error.code === 400)) return false;
      this.log?.warn('Could not write the final status', { message: error.message });
      return false;
    }
  }
}

/** Maps an internal failure to something a normal person can act on. */
export function explainError(error, { maxUploadMb = 50, quality = '720p' } = {}) {
  const message = String(error?.message ?? error ?? 'Unknown error');

  if (error instanceof CancelledError || error?.code === 'CANCELLED') {
    return { text: '🛑 Download cancelled.', retryQualities: [] };
  }

  if (error instanceof TooLargeError || (error instanceof TelegramApiError && error.isTooLarge)) {
    const from = error.quality ?? quality;
    const lower = nextLowerQuality(from) ?? '360p';
    const tried = error.tried?.length ? ` Already retried at ${error.tried.join(', ')}.` : '';
    const size = error.bytes ? ` is ${formatMb(error.bytes)}` : '';
    // When the worker runs without a limit of its own, the only ceiling left is
    // Telegram's: 50 MB on the hosted Bot API, 2 GB on a self-hosted server.
    const limit =
      Number.isFinite(maxUploadMb) && maxUploadMb > 0
        ? `above Telegram's ${maxUploadMb} MB upload limit for bots`
        : "above Telegram's upload limit (50 MB on the hosted Bot API, 2 GB on a self-hosted server)";
    return {
      text: `⚠️ The finished file${size}, ${limit}.${tried} Pick a smaller quality below, or send /download ${lower} <URL>.`,
      retryQualities: smallerQualities(from, 3)
    };
  }

  if (error?.code === 'UNSUPPORTED_RUNTIME') {
    return {
      text:
        '⚙️ The worker runs an unsupported Node.js version, so TypeTube is unavailable and the yt-dlp fallback ' +
        'is switched off. Redeploy the worker on Node.js 22.15 or newer.',
      retryQualities: []
    };
  }

  if (error?.code === 'YTDLP_MISSING') {
    return {
      text: '⚙️ Neither TypeTube nor yt-dlp could run on the worker. Check the worker logs for details.',
      retryQualities: []
    };
  }

  if (error?.code === 'TIMEOUT') {
    return {
      text: `⏱️ ${message} Try again, choose a lower quality, or use a shorter video.`,
      retryQualities: smallerQualities(quality, 2)
    };
  }

  if (/403/.test(message)) {
    return {
      text:
        "🚫 YouTube refused the download for the worker's region (HTTP 403). This mostly affects some music and " +
        'VEVO tracks. Try a different video, or host the worker in Singapore, which is the region TypeTube serves best.',
      retryQualities: []
    };
  }

  if (/private video|members-only|age restricted|sign in to confirm/i.test(message)) {
    return { text: `🔒 ${message}`, retryQualities: [] };
  }

  if (/no space left/i.test(message)) {
    return { text: '💾 The worker ran out of disk space. Try again in a minute.', retryQualities: [] };
  }

  if (/queue is full/i.test(message)) {
    return { text: '🕓 The worker is busy right now. Please try again in a few minutes.', retryQualities: [] };
  }

  return { text: `❌ Download failed: ${message.slice(0, 300)}`, retryQualities: [] };
}

/** The ladder of smaller qualities offered after a "file too large" failure. */
export function smallerQualities(quality, count) {
  const out = [];
  let current = quality;
  for (let index = 0; index < count; index += 1) {
    const lower = nextLowerQuality(current);
    if (!lower) break;
    out.push(lower);
    current = lower;
  }
  return out;
}

/** Inline keyboard offering smaller qualities (the classic 50 MB problem). */
export function retryKeyboard(jobId, qualities) {
  const usable = qualities.filter((item) => QUALITIES.includes(item) && heightOf(item) > 0);
  if (usable.length === 0) return undefined;
  return {
    inline_keyboard: [usable.slice(0, 3).map((item) => ({ text: `⬇️ Retry ${item}`, callback_data: `dl:${item}:${jobId}` }))]
  };
}

function buildCaption(result) {
  const title = result.title || (result.mode === 'audio' ? 'Audio' : 'Video');
  const details = [];
  if (result.formatLabel) details.push(result.formatLabel);
  if (result.sizeBytes) details.push(formatMb(result.sizeBytes));
  if (result.source) details.push(result.source === 'typetube' ? 'via TypeTube' : 'via yt-dlp');
  if (result.hasAudio === false) details.push('no audio track');

  const heading = result.mode === 'audio' ? `🎵 ${title}` : `🎬 ${title}`;
  return details.length > 0 ? `${heading}\n${details.join(' · ')}` : heading;
}

async function safeThumbnail(result, url, dir, log) {
  if (result.mode !== 'video') return null;
  try {
    return await fetchThumbnail({ url, targetDir: dir, preferredUrl: result.thumbnailUrl, log });
  } catch (error) {
    log.debug('Thumbnail skipped', { message: error.message });
    return null;
  }
}

/**
 * @param {object} job Queue job record.
 * @param {object} deps
 * @param {object} deps.config
 * @param {import('./logger.js').Logger} deps.log
 * @param {import('./telegram.js').TelegramClient} deps.telegram
 * @param {Downloader} deps.downloader
 * @returns {Promise<'done'|'cancelled'>}
 */
export async function processJob(job, { config, log, telegram, downloader }) {
  const { id, chatId, statusMessageId, url, quality, mode, signal } = job;
  const jobLog = log.child({ jobId: id, chatId, mode, quality });

  const status = statusMessageId
    ? new StatusUpdater({
        telegram,
        chatId,
        messageId: statusMessageId,
        log: jobLog,
        minIntervalMs: config.STATUS_UPDATE_SECONDS * 1000
      })
    : null;

  let dir = null;
  let actionTimer = null;

  try {
    dir = await createJobDir();
    jobLog.info('Job started');

    await status?.update('🔎 Preparing the download…', { force: true });

    // A periodic chat action keeps the "uploading…" spinner alive, which is the only
    // progress signal Telegram shows on mobile.
    const chatAction = mode === 'audio' ? 'upload_voice' : 'upload_video';
    actionTimer = setInterval(() => {
      void telegram.sendChatAction(chatId, chatAction);
    }, CHAT_ACTION_INTERVAL_MS);
    actionTimer.unref?.();

    const result = await downloader.download({
      url,
      quality,
      mode,
      dir,
      signal,
      onProgress: (progress) => {
        if (progress?.text) void status?.update(progress.text);
      }
    });

    if (signal.aborted) throw new CancelledError();

    jobLog.info('Download finished', {
      source: result.source,
      sizeBytes: result.sizeBytes,
      format: result.formatLabel
    });

    await status?.update(`⬆️ Uploading ${formatMb(result.sizeBytes)} to Telegram…`, { force: true });
    await telegram.sendChatAction(chatId, chatAction);

    const caption = buildCaption(result);
    const thumbnailPath = await safeThumbnail(result, url, dir, jobLog);

    if (result.mode === 'audio' && result.containerIsMusic !== false) {
      await telegram.sendAudioFile({
        chatId,
        filePath: result.filePath,
        title: result.title,
        performer: result.performer,
        duration: result.durationSeconds,
        caption,
        signal
      });
    } else if (result.mode === 'audio') {
      // Audio the Bot API will not treat as music (Opus/WebM without FFmpeg).
      await telegram.sendDocumentFile({
        chatId,
        filePath: result.filePath,
        caption,
        filename: basename(result.filePath),
        contentType: 'audio/ogg',
        signal
      });
    } else {
      await telegram.sendVideoFile({
        chatId,
        filePath: result.filePath,
        caption,
        duration: result.durationSeconds,
        width: result.width,
        height: result.height,
        thumbnailPath,
        signal
      });
    }

    if (statusMessageId) await telegram.deleteMessage(chatId, statusMessageId);
    jobLog.info('Job delivered', { sizeBytes: result.sizeBytes, source: result.source });
    return 'done';
  } catch (error) {
    const cancelled = error instanceof CancelledError || error?.code === 'CANCELLED' || signal.aborted;

    if (cancelled) {
      jobLog.info('Job cancelled');
      await status?.final('🛑 Cancelled. Send the link again whenever you are ready.');
      return 'cancelled';
    }

    jobLog.error('Job failed', { code: error?.code, message: error?.message, source: error?.source });

    const { text, retryQualities } = explainError(error, { maxUploadMb: config.MAX_UPLOAD_MB, quality });
    await status?.final(text, retryKeyboard(id, retryQualities));
    return 'done';
  } finally {
    if (actionTimer) clearInterval(actionTimer);
    await removeJobDir(dir, jobLog);
  }
}

export { StatusUpdater, buildCaption };
