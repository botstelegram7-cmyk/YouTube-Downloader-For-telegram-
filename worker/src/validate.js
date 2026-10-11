/**
 * Input validation shared by the worker.
 *
 * `tgcloud/lib/youtube.js` keeps a runtime-identical copy of the URL and command
 * rules because the Serverless runtime cannot import this file. The two are held
 * together by `test/fixtures/url_vectors.json`, which is executed by both test
 * suites (see tests/youtube.test.mjs).
 */

/** Hosts we accept. Anything else — including lookalikes — is rejected. */
export const YOUTUBE_HOSTS = Object.freeze([
  'youtube.com',
  'www.youtube.com',
  'm.youtube.com',
  'music.youtube.com',
  'youtu.be',
  'www.youtu.be'
]);

/** Video heights offered to users, ascending. */
export const QUALITIES = Object.freeze(['360p', '480p', '720p', '1080p', '1440p', '2160p']);

export const MODES = Object.freeze(['video', 'audio']);

export const DEFAULT_QUALITY = '720p';
export const DEFAULT_MODE = 'video';

const HOST_SET = new Set(YOUTUBE_HOSTS);
const MAX_URL_LENGTH = 2048;

/**
 * Accepts a YouTube URL in any common shape and returns a canonical https URL,
 * or `null` when the value is not a YouTube video link.
 *
 * @param {unknown} value
 * @returns {string | null}
 */
export function normalizeYouTubeUrl(value) {
  if (typeof value !== 'string') return null;
  const input = value.trim();
  if (!input || input.length > MAX_URL_LENGTH) return null;

  try {
    const candidate = /^[a-z][a-z\d+.-]*:\/\//i.test(input) ? input : `https://${input}`;
    const url = new URL(candidate);

    if (!['http:', 'https:'].includes(url.protocol)) return null;
    if (!HOST_SET.has(url.hostname.toLowerCase())) return null;
    // Credentials in the URL are a sign of an attempt to confuse host parsing.
    if (url.username || url.password) return null;
    if (!url.pathname.split('/').some(Boolean)) return null;

    url.protocol = 'https:';
    const canonical = url.toString();
    // A link is only useful when it points at one specific video: playlists,
    // channel pages and lookalikes carry no 11-character id.
    if (!youtubeVideoId(canonical)) return null;

    return canonical;
  } catch {
    return null;
  }
}

/** @param {unknown} value */
export function isQuality(value) {
  return typeof value === 'string' && QUALITIES.includes(value);
}

/** @param {unknown} value */
export function isMode(value) {
  return typeof value === 'string' && MODES.includes(value);
}

/** `"1080p"` → `1080`. Unknown shapes → 0. */
export function heightOf(quality) {
  const match = /^(\d{2,4})p$/.exec(String(quality ?? '').trim());
  return match ? Number(match[1]) : 0;
}

/** The next smaller offered quality, or `null` when already at the bottom. */
export function nextLowerQuality(quality) {
  const height = heightOf(quality);
  if (!height) return null;
  const lower = QUALITIES.filter((item) => heightOf(item) < height);
  return lower.length > 0 ? lower[lower.length - 1] : null;
}

/** Extracts the 11-character video id, when the URL carries one. */
export function youtubeVideoId(url) {
  if (typeof url !== 'string') return null;
  try {
    const parsed = new URL(url);
    const host = parsed.hostname.toLowerCase();
    if (host === 'youtu.be' || host === 'www.youtu.be') {
      const id = parsed.pathname.split('/').filter(Boolean)[0] ?? '';
      return /^[\w-]{11}$/.test(id) ? id : null;
    }
    const v = parsed.searchParams.get('v');
    if (v && /^[\w-]{11}$/.test(v)) return v;
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
 * A JPEG thumbnail URL is required to be ≤ 200 kB by the Bot API; `mqdefault.jpg`
 * is 320×180 and comfortably inside those bounds.
 */
export function thumbnailFor(videoId, variant = 'mqdefault') {
  if (!videoId || !/^[\w-]{11}$/.test(videoId)) return null;
  const allowed = new Set(['default', 'mqdefault', 'hqdefault', 'sddefault']);
  const size = allowed.has(variant) ? variant : 'mqdefault';
  return `https://i.ytimg.com/vi/${videoId}/${size}.jpg`;
}

/** Filename that is safe on every filesystem Telegram and Render might use. */
export function safeFileName(value, fallback = 'video', maxLength = 90) {
  const cleaned = String(value ?? '')
    .replace(/[\u0000-\u001f\u007f]/g, '')
    .replace(/[\\/:*?"<>|]+/g, ' ')
    .replace(/\s+/g, ' ')
    .replace(/^[.\s]+|[.\s]+$/g, '')
    .trim();
  const base = cleaned || fallback;
  return base.length > maxLength ? base.slice(0, maxLength).trim() : base;
}
