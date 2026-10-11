/**
 * Pure URL and command logic for the Serverless bot.
 *
 * Runtime rules that shape this file:
 *   - it must not import anything (not even the SDK), so it stays unit-testable in
 *     plain Node and safe to load in the V8 sandbox;
 *   - `worker/src/validate.js` holds the same rules for the download worker, and the
 *     two are kept honest by `worker/test/fixtures/url_vectors.json`, which both test
 *     suites run against (see tests/youtube.test.mjs).
 */

export const YOUTUBE_HOSTS = [
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtu.be',
  'www.youtu.be'
];

export const QUALITIES = ['360p', '480p', '720p', '1080p', '1440p', '2160p'];
export const MODES = ['video', 'audio'];
export const DEFAULT_QUALITY = '720p';
export const DEFAULT_MODE = 'video';

const MAX_URL_LENGTH = 2048;

/** Extracts the 11-character video id, or null when there is none. */
export function youtubeVideoId(url) {
  if (typeof url !== 'string') return null;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (host === 'youtu.be' || host === 'www.youtu.be') {
      const id = parsed.pathname.split('/').filter(Boolean)[0] ?? '';
      return /^[\w-]{11}$/.test(id) ? id : null;
    }
    const fromQuery = parsed.searchParams.get('v');
    if (fromQuery && /^[\w-]{11}$/.test(fromQuery)) return fromQuery;
    const parts = parsed.pathname.split('/').filter(Boolean);
    if (parts.length >= 2 && ['shorts', 'embed', 'live', 'v'].includes(parts[0])) {
      return /^[\w-]{11}$/.test(parts[1]) ? parts[1] : null;
    }
    return null;
  } catch {
    return null;
  }
}

/**
 * Returns a canonical https YouTube URL for a single video, or null.
 * @param {unknown} value
 * @returns {string|null}
 */
export function normalizeYouTubeUrl(value) {
  if (typeof value !== 'string') return null;
  const input = value.trim();
  if (!input || input.length > MAX_URL_LENGTH) return null;

  try {
    const candidate = /^[a-z][a-z\d+.-]*:\/\//i.test(input) ? input : `https://${input}`;
    const url = new URL(candidate);

    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    if (!YOUTUBE_HOSTS.includes(url.hostname.toLowerCase())) return null;
    if (url.username || url.password) return null;
    if (!url.pathname.split('/').some(Boolean)) return null;

    url.protocol = 'https:';
    const canonical = url.toString();
    if (!youtubeVideoId(canonical)) return null;

    return canonical;
  } catch {
    return null;
  }
}

/**
 * Splits `"/start@Bot 720p"` into `{ command: '/start', args: ['720p'] }`.
 * Ordinary text — anything not starting with `/` — yields an empty command and keeps
 * its words as args, so callers can tell "not a command" apart from "unknown command".
 */
export function parseCommand(text) {
  const trimmed = typeof text === 'string' ? text.trim() : '';
  if (!trimmed) return { command: '', args: [] };

  const parts = trimmed.split(/\s+/);
  const rawCommand = parts[0] ?? '';
  if (!rawCommand.startsWith('/')) return { command: '', args: parts };

  return { command: rawCommand.split('@')[0].toLowerCase(), args: parts.slice(1) };
}

export function isQuality(value) {
  return typeof value === 'string' && QUALITIES.includes(value);
}

export function isMode(value) {
  return typeof value === 'string' && MODES.includes(value);
}

/** `"1080p"` → `1080`; anything else → 0. */
export function heightOf(quality) {
  const match = /^(\d{2,4})p$/.exec(String(quality ?? '').trim());
  return match ? Number(match[1]) : 0;
}

/** The next smaller offered quality, or null. */
export function nextLowerQuality(quality) {
  const height = heightOf(quality);
  if (!height) return null;
  const lower = QUALITIES.filter((item) => heightOf(item) < height);
  return lower.length > 0 ? lower[lower.length - 1] : null;
}

/** `360p` and below are the only qualities that reliably fit Telegram's 50 MB cap. */
export function isSmallQuality(quality) {
  const height = heightOf(quality);
  return height > 0 && height <= 480;
}

/**
 * callback_data is capped at 64 bytes, so it stays to short ASCII fields:
 * `q:<quality>`, `cancel:<jobId>`, `dl:<quality>:<jobId>`, `help`.
 */
export function encodeCallbackData(parts) {
  return parts.map((part) => String(part)).join(':').slice(0, 64);
}

export function decodeCallbackData(data) {
  if (typeof data !== 'string' || data.length === 0 || data.length > 64) return null;
  const [action, ...rest] = data.split(':');
  switch (action) {
    case 'q':
      return isQuality(rest[0]) ? { action: 'quality', quality: rest[0] } : null;
    case 'cancel':
      return rest[0] ? { action: 'cancel', jobId: rest[0] } : null;
    case 'dl':
      return isQuality(rest[0]) && rest[1] ? { action: 'download', quality: rest[0], jobId: rest[1] } : null;
    case 'help':
      return { action: 'help' };
    case 'status':
      return { action: 'status' };
    default:
      return null;
  }
}

/** Finds the first YouTube link inside a message (people paste text around links). */
export function findYouTubeUrl(text) {
  if (typeof text !== 'string') return null;
  const candidates = text.split(/\s+/).filter((part) => part.length > 0);
  for (const candidate of candidates) {
    const normalized = normalizeYouTubeUrl(candidate.replace(/^<|>$/g, ''));
    if (normalized) return normalized;
  }
  return normalizeYouTubeUrl(text);
}
