/**
 * Every string the bot sends to a person.
 *
 * Keeping copy in one module makes it reviewable, translatable and consistent, and
 * keeps the handlers about logic only. HTML is used for formatting because it is far
 * less error-prone than Markdown when titles contain `_`, `*` or `[`.
 */

import { QUALITIES } from './youtube.js';

/** Escapes the three characters Telegram's HTML parser treats as markup. */
export function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;');
}

export function welcomeBot(config) {
  return (
    '🎬 <b>YouTube Downloader</b>\n\n' +
    'Send me a YouTube link and I will download it with <b>TypeTube</b> and deliver it here.\n\n' +
    '<b>Quick start</b>\n' +
    '• Send a video, Shorts or youtu.be link\n' +
    '• /quality — pick a resolution\n' +
    '• /audio — get the audio track only\n' +
    '• /help — everything else\n\n' +
    `<i>${escapeHtml(config.DISCLAIMER)}</i>`
  );
}

export function helpText({ config, quality, mode, configured, isOwner: owner }) {
  const lines = [
    '📖 <b>How to use this bot</b>',
    '',
    '<b>Send a link</b> — a YouTube video, Shorts or youtu.be URL. I reply with the file.',
    '',
    '<b>Commands</b>',
    '/quality — choose the resolution (' + QUALITIES.join(', ') + ')',
    '/audio — audio only (M4A)',
    '/video — video with sound (default)',
    '/download 480p &lt;link&gt; — one-off quality for a single link',
    '/status — worker health and your running job',
    '/cancel — stop your running download',
    '/about — credits, limits and the legal note'
  ];

  if (owner) {
    lines.push(
      '',
      '<b>Owner commands</b>',
      '/setup &lt;worker-url&gt; &lt;secret&gt; — connect the Render worker',
      '/config — show the current configuration',
      '/stats — queue and runtime details',
      '/unset — forget the worker credentials'
    );
  }

  lines.push(
    '',
    `<b>Your settings</b>: ${quality} · ${mode}`,
    `<b>Worker</b>: ${configured ? 'connected' : 'not configured yet'}`,
    '',
    `⚠️ Telegram limits bot uploads to <b>50 MB</b> per file. Larger videos need a lower quality, and I will offer you buttons to retry smaller.`,
    '',
    `<i>${escapeHtml(config.DISCLAIMER)}</i>`
  );

  return lines.join('\n');
}

export function aboutText(config) {
  return (
    'ℹ️ <b>About</b>\n\n' +
    'This bot runs on <b>Telegram Serverless</b> (handlers + database inside Telegram) and a small ' +
    '<b>download worker</b> on Render that uses <b>TypeTube</b>, with yt-dlp only as a fallback.\n\n' +
    '<b>Limits</b>\n' +
    '• 50 MB per file through the Telegram Bot API\n' +
    '• One download at a time per person\n' +
    '• TypeTube’s public cluster allows 60 lookups per minute and 1,500 per day per IP\n' +
    '• Some music videos are region-restricted by YouTube (HTTP 403)\n\n' +
    `<i>${escapeHtml(config.DISCLAIMER)}</i>`
  );
}

export function qualityPicker(current, mode) {
  return {
    text:
      `🎚️ <b>Choose a quality</b>\n\nCurrent: <b>${current}</b> · mode: <b>${mode}</b>\n\n` +
      'Lower qualities are much more likely to fit Telegram’s 50 MB upload limit.',
    keyboard: {
      inline_keyboard: [
        QUALITIES.slice(0, 3).map((item) => button(item, `q:${item}`, item === current)),
        QUALITIES.slice(3).map((item) => button(item, `q:${item}`, item === current)),
        [{ text: 'ℹ️ Help', callback_data: 'help' }]
      ]
    }
  };
}

function button(label, data, active) {
  return { text: active ? `✅ ${label}` : label, callback_data: data };
}

export function qualitySaved(quality, mode) {
  return `✅ Saved. Downloads will use <b>${quality}</b> (${mode}).\n\nSend a YouTube link whenever you are ready.`;
}

export function modeSaved(mode) {
  return mode === 'audio'
    ? '🎵 Audio mode on — I will send the soundtrack as an M4A file.'
    : '🎬 Video mode on — I will send the video with sound.';
}

export function queued({ position, quality, mode, etaHint }) {
  const where = position > 1 ? `Queued at position ${position}` : 'Starting now';
  return (
    `⏳ <b>${where}</b>\n` +
    `Quality: <b>${quality}</b> · mode: <b>${mode}</b>\n` +
    `${etaHint ? `${etaHint}\n` : ''}` +
    'Use the button below to stop it at any time.'
  );
}

export function alreadyRunning() {
  return '⌛️ You already have a download running. Wait for it to finish or press <b>Cancel</b>.';
}

export function busy() {
  return '🕓 The worker is finishing other downloads. Please try again in a minute.';
}

export function notConfigured() {
  return (
    '🔧 <b>The bot is not connected to a download worker yet.</b>\n\n' +
    'An administrator needs to run:\n' +
    '<code>/setup https://your-worker.onrender.com YOUR_SECRET</code>\n\n' +
    'See <code>docs/DEPLOYMENT.md</code> for the step-by-step guide.'
  );
}

export function ownerOnly() {
  return '🔒 That command is only available to the bot owner.';
}

export function invalidLink() {
  return (
    '❓ That does not look like a YouTube video link.\n\n' +
    'Send something like <code>https://youtu.be/jNQXAC9IVRw</code>, or use ' +
    '<code>/download 480p &lt;link&gt;</code>.'
  );
}

export function cooldown(seconds) {
  return `🐢 Slow down a little — you can start another download in ${seconds}s.`;
}

export function dailyLimitReached(limit) {
  return `🚦 You have reached today's limit of ${limit} downloads. Try again tomorrow.`;
}

export function cancelled() {
  return '🛑 Cancelled.';
}

export function nothingToCancel() {
  return '🤷 There is no download running for you right now.';
}

export function setupMissingArgs() {
  return (
    'Usage: <code>/setup https://worker.example.com YOUR_SECRET</code>\n\n' +
    'Find the URL on your Render service page and the secret in its environment variables. ' +
    'I delete your message right after saving, and the secret is stored in the bot database — never in git.'
  );
}

export function setupSaved(url) {
  return `✅ Worker connected: <code>${escapeHtml(url)}</code>\n\nSend a YouTube link to try it.`;
}

export function setupCleared() {
  return '🧹 Worker credentials removed. The bot can no longer download until /setup runs again.';
}

export function configSummary({ url, secretMasked, ownerId, quality, mode, configured }) {
  return (
    '⚙️ <b>Configuration</b>\n\n' +
    `Worker URL: <code>${escapeHtml(url || '—')}</code>\n` +
    `Worker secret: <code>${escapeHtml(secretMasked || '—')}</code>\n` +
    `Owner id: <code>${escapeHtml(ownerId || 'unclaimed')}</code>\n` +
    `Defaults: ${quality} · ${mode}\n` +
    `Connected: ${configured ? 'yes' : 'no'}`
  );
}

export function workerError(error) {
  switch (error?.kind) {
    case 'unauthorized':
      return (
        '🔑 The worker rejected the shared secret.\n\n' +
        'Re-run <code>/setup &lt;url&gt; &lt;secret&gt;</code> with the exact <code>WORKER_SECRET</code> from the Render dashboard.'
      );
    case 'unreachable':
      return (
        '😴 The worker did not answer in time.\n\n' +
        'On Render’s free plan it sleeps after 15 minutes and needs about 50 seconds to wake up. ' +
        'Send the link again in a minute — or set up the keep-alive ping from <code>docs/DEPLOYMENT.md</code>.'
      );
    case 'busy':
      return busy();
    case 'duplicate':
      return alreadyRunning();
    case 'badRequest':
      return '⚠️ The worker rejected this request. Try another link or a different quality.';
    default:
      return '❌ The worker returned an unexpected error. Check the worker logs on Render.';
  }
}

export function statusText({ health, job }) {
  const lines = ['📊 <b>Status</b>', ''];

  if (!health) {
    lines.push('Worker: <b>unreachable</b> (probably asleep — send a link to wake it)');
  } else {
    lines.push(
      `Worker: <b>online</b> · v${escapeHtml(health.version ?? '?')}`,
      `Runtime: Node ${escapeHtml(health.runtime?.node ?? '?')} · FFmpeg ${escapeHtml(health.runtime?.ffmpeg ?? '?')}`,
      `TypeTube: <b>${health.typeTube?.supported ? 'available' : 'unavailable'}</b>`,
      `Queue: ${health.queue?.running ?? 0} running · ${health.queue?.pending ?? 0} waiting`
    );
  }

  lines.push('');
  lines.push(
    job
      ? `Your job: <code>${escapeHtml(job.id)}</code> · ${escapeHtml(job.quality)} · ${escapeHtml(job.status)}`
      : 'Your job: none running'
  );

  return lines.join('\n');
}

export function delivered() {
  return '✅ Sent.';
}

export function disclaimerReminder() {
  return '⚠️ Reminder: download only what you are allowed to save.';
}
