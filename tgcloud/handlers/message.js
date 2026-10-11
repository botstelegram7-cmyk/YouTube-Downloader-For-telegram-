/**
 * message handler — every update that carries a `message` arrives here.
 *
 * Serverless contract: `export default async (input, ctx)` where `input` is the
 * Message itself (not the Update) and `ctx.update` is the full Update. Relative
 * imports need the `.js` extension; the SDK is imported by name.
 */

import { api } from 'sdk';
import { CONFIG, SETTING_KEYS } from '../lib/config.js';
import { findYouTubeUrl, isMode, isQuality, parseCommand, QUALITIES } from '../lib/youtube.js';
import * as M from '../lib/messages.js';
import { sendMessage, startDownload, stopDownload } from '../lib/download_flow.js';
import {
  claimOwnership,
  deleteSetting,
  getActiveJob,
  getPreferences,
  getSetting,
  resolveOwnerId,
  resolveWorkerConfig,
  savePreferences,
  setSetting
} from '../lib/storage.js';
import { fetchHealth, isValidWorkerUrl, maskSecret, normalizeBaseUrl } from '../lib/worker_api.js';

export default async function handleMessage(message) {
  if (!message?.chat?.id) return;

  const chatId = message.chat.id;
  const userId = message.from?.id ?? 0;
  const text = typeof message.text === 'string' ? message.text : '';

  // Photos, stickers and voice notes carry nothing we can act on.
  if (!text) return;

  const { command, args } = parseCommand(text);
  const ownerId = await resolveOwnerId(CONFIG.OWNER_USER_ID);
  const isOwnerViewer = ownerId === 0 || userId === ownerId;

  switch (command) {
    case '/start':
      await sendMessage(chatId, M.welcomeBot(CONFIG));
      return;

    case '/help': {
      const preferences = await getPreferences(userId);
      await sendMessage(
        chatId,
        M.helpText({
          config: CONFIG,
          quality: preferences.quality,
          mode: preferences.mode,
          configured: Boolean(await resolveWorkerConfig(CONFIG)),
          isOwner: isOwnerViewer
        })
      );
      return;
    }

    case '/about':
      await sendMessage(chatId, M.aboutText(CONFIG));
      return;

    case '/quality':
      await handleQuality({ chatId, userId, args });
      return;

    case '/audio':
      await savePreferences(userId, { mode: 'audio' });
      await sendMessage(chatId, M.modeSaved('audio'));
      return;

    case '/video':
      await savePreferences(userId, { mode: 'video' });
      await sendMessage(chatId, M.modeSaved('video'));
      return;

    case '/mode': {
      const requested = (args[0] ?? '').toLowerCase();
      if (!isMode(requested)) {
        await sendMessage(chatId, 'Usage: <code>/mode video</code> or <code>/mode audio</code>');
        return;
      }
      await savePreferences(userId, { mode: requested });
      await sendMessage(chatId, M.modeSaved(requested));
      return;
    }

    case '/download':
      await handleExplicitDownload({ chatId, userId, args, message });
      return;

    case '/status':
      await handleStatus({ chatId, userId });
      return;

    case '/cancel':
      await handleCancel({ chatId, userId });
      return;

    case '/setup':
      await handleSetup({ message, chatId, userId, args, ownerId });
      return;

    case '/unset':
      await handleUnset({ chatId, userId, ownerId });
      return;

    case '/config':
      await handleConfig({ chatId, userId, ownerId });
      return;

    case '/stats':
      await handleStats({ chatId, userId, ownerId });
      return;

    default:
      break;
  }

  const url = findYouTubeUrl(text);
  if (!url) {
    await sendMessage(
      chatId,
      command ? `🤔 I do not know <code>${M.escapeHtml(command)}</code>. Try /help.` : M.invalidLink()
    );
    return;
  }

  const preferences = await getPreferences(userId);
  const result = await startDownload({
    chatId,
    userId,
    url,
    quality: preferences.quality,
    mode: preferences.mode,
    replyToMessageId: message.message_id,
    config: CONFIG
  });

  if (!result.ok && !result.reported) await sendMessage(chatId, result.message);
}

async function handleQuality({ chatId, userId, args }) {
  const requested = (args[0] ?? '').toLowerCase();
  const current = await getPreferences(userId);

  if (!requested) {
    const picker = M.qualityPicker(current.quality, current.mode);
    await sendMessage(chatId, picker.text, { reply_markup: picker.keyboard });
    return;
  }

  if (!isQuality(requested)) {
    await sendMessage(chatId, `Usage: <code>/quality 720p</code>\nOptions: ${QUALITIES.join(', ')}`);
    return;
  }

  const saved = await savePreferences(userId, { quality: requested });
  await sendMessage(chatId, M.qualitySaved(saved.quality, saved.mode));
}

async function handleExplicitDownload({ chatId, userId, args, message }) {
  const quality = (args[0] ?? '').toLowerCase();
  const candidate = args.slice(1).join(' ');

  if (!isQuality(quality) || !candidate) {
    await sendMessage(
      chatId,
      'Usage: <code>/download 480p https://youtu.be/VIDEO_ID</code>\n\n' +
        `Qualities: ${QUALITIES.join(', ')}\n` +
        'Tip: 360p–480p are the ones that reliably fit Telegram’s 50 MB limit.'
    );
    return;
  }

  const url = findYouTubeUrl(candidate);
  if (!url) {
    await sendMessage(chatId, M.invalidLink());
    return;
  }

  const preferences = await getPreferences(userId);
  const result = await startDownload({
    chatId,
    userId,
    url,
    quality,
    mode: preferences.mode,
    replyToMessageId: message.message_id,
    config: CONFIG
  });

  if (!result.ok && !result.reported) await sendMessage(chatId, result.message);
}

async function handleStatus({ chatId, userId }) {
  const worker = await resolveWorkerConfig(CONFIG);
  const health = worker ? await fetchHealth({ workerUrl: worker.url }) : null;
  const job = await getActiveJob(userId);
  await sendMessage(chatId, M.statusText({ health, job }));
}

async function handleCancel({ chatId, userId }) {
  const job = await getActiveJob(userId);
  if (!job) {
    await sendMessage(chatId, M.nothingToCancel());
    return;
  }

  const result = await stopDownload({ jobId: job.id, userId, config: CONFIG });
  if (!result.ok) await sendMessage(chatId, result.message);
}

async function handleSetup({ message, chatId, userId, args, ownerId }) {
  // The first /setup claims ownership when OWNER_USER_ID is left at 0.
  if (ownerId !== 0 && userId !== ownerId) {
    await sendMessage(chatId, M.ownerOnly());
    return;
  }

  if (args.length < 2) {
    await sendMessage(chatId, M.setupMissingArgs());
    return;
  }

  const [rawUrl, secret] = [args[0], args[1]];

  if (!isValidWorkerUrl(rawUrl)) {
    await sendMessage(
      chatId,
      '❌ That does not look like a worker URL.\n\nExpected the Render service URL, for example ' +
        '<code>https://youtube-dl-worker.onrender.com</code> — https, and no <code>/download</code> suffix.'
    );
    return;
  }

  if (secret.length < 16) {
    await sendMessage(chatId, '❌ That secret looks too short. Use the full <code>WORKER_SECRET</code> value.');
    return;
  }

  const baseUrl = normalizeBaseUrl(rawUrl);
  await setSetting(SETTING_KEYS.WORKER_URL, baseUrl);
  await setSetting(SETTING_KEYS.WORKER_SECRET, secret);
  if (ownerId === 0) await claimOwnership(userId);

  // Keep the credentials out of the chat history.
  try {
    await api.deleteMessage({ chat_id: chatId, message_id: message.message_id });
  } catch (error) {
    console.log('Could not delete the /setup message', { message: error?.description ?? error?.message });
  }

  const storedSecret = await getSetting(SETTING_KEYS.WORKER_SECRET);
  const health = await fetchHealth({ workerUrl: baseUrl });

  await sendMessage(
    chatId,
    M.setupSaved(baseUrl) +
      (health
        ? `\n\nWorker answered: Node ${M.escapeHtml(health.runtime?.node ?? '?')} · ` +
          `FFmpeg ${M.escapeHtml(health.runtime?.ffmpeg ?? '?')} · ` +
          `TypeTube ${health.typeTube?.supported ? 'available' : 'unavailable'}`
        : '\n\n⚠️ The worker has not answered yet. On Render’s free plan it may be waking up — try a link in a minute.') +
      `\n\nSecret stored (${maskSecret(storedSecret)}).`
  );
}

async function handleUnset({ chatId, userId, ownerId }) {
  if (ownerId !== 0 && userId !== ownerId) {
    await sendMessage(chatId, M.ownerOnly());
    return;
  }

  await deleteSetting(SETTING_KEYS.WORKER_URL);
  await deleteSetting(SETTING_KEYS.WORKER_SECRET);
  await sendMessage(chatId, M.setupCleared());
}

async function handleConfig({ chatId, userId, ownerId }) {
  if (ownerId !== 0 && userId !== ownerId) {
    await sendMessage(chatId, M.ownerOnly());
    return;
  }

  const worker = await resolveWorkerConfig(CONFIG);
  const preferences = await getPreferences(userId);
  const currentOwner = await resolveOwnerId(CONFIG.OWNER_USER_ID);

  await sendMessage(
    chatId,
    M.configSummary({
      url: worker?.url ?? '',
      secretMasked: worker ? maskSecret(worker.secret) : '',
      ownerId: currentOwner || '',
      quality: preferences.quality,
      mode: preferences.mode,
      configured: Boolean(worker)
    })
  );
}

async function handleStats({ chatId, userId, ownerId }) {
  if (ownerId !== 0 && userId !== ownerId) {
    await sendMessage(chatId, M.ownerOnly());
    return;
  }

  const worker = await resolveWorkerConfig(CONFIG);
  if (!worker) {
    await sendMessage(chatId, M.notConfigured());
    return;
  }

  const health = await fetchHealth({ workerUrl: worker.url });
  if (!health) {
    await sendMessage(chatId, '😴 The worker did not answer — it is probably asleep. Send a link to wake it.');
    return;
  }

  const queue = health.queue ?? {};
  await sendMessage(
    chatId,
    [
      '📈 <b>Worker stats</b>',
      '',
      `Version: <code>${M.escapeHtml(health.version ?? '?')}</code>`,
      `Uptime: ${Math.round((health.uptimeSeconds ?? 0) / 60)} min`,
      `Running: ${queue.running ?? 0} · waiting: ${queue.pending ?? 0} · limit: ${queue.concurrency ?? '?'}`,
      `Memory: ${health.runtime?.memoryMb ?? '?'} MB · free disk: ${health.runtime?.freeDiskMb ?? '?'} MB`,
      `TypeTube: ${health.typeTube?.supported ? 'available' : 'unavailable'} (zstd: ${health.typeTube?.zstd ? 'yes' : 'no'})`,
      `FFmpeg: <code>${M.escapeHtml(health.runtime?.ffmpeg ?? 'missing')}</code>`
    ].join('\n')
  );
}
