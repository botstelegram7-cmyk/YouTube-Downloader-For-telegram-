/**
 * Video thumbnails.
 *
 * Telegram shows a nicer preview when `sendVideo` receives a `thumbnail` file:
 * JPEG, ≤ 200 kB, ≤ 320 px on the long side. YouTube's `mqdefault.jpg` (320×180)
 * satisfies all three, so we only fetch and size-check it, never resize it.
 */

import fs from 'node:fs';
import path from 'node:path';
import { thumbnailFor, youtubeVideoId } from './validate.js';

const MAX_THUMBNAIL_BYTES = 200 * 1024;
const FETCH_TIMEOUT_MS = 8_000;

/** JPEG SOI marker, so we never hand Telegram something that is not an image. */
function isJpeg(buffer) {
  return buffer.length > 3 && buffer[0] === 0xff && buffer[1] === 0xd8 && buffer[2] === 0xff;
}

/**
 * @param {object} options
 * @param {string} options.url The video URL whose thumbnail should be fetched.
 * @param {string} options.targetDir Directory to write `thumb.jpg` into.
 * @param {string} [options.preferredUrl] A thumbnail URL reported by the resolver.
 * @returns {Promise<string|null>} path to a usable JPEG, or null
 */
export async function fetchThumbnail({ url, targetDir, preferredUrl, log }) {
  const videoId = youtubeVideoId(url);
  const candidates = [preferredUrl, thumbnailFor(videoId, 'mqdefault'), thumbnailFor(videoId, 'default')].filter(
    Boolean
  );

  for (const candidate of candidates) {
    try {
      const parsed = new URL(candidate);
      if (!['http:', 'https:'].includes(parsed.protocol)) continue;

      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
      let response;
      let buffer;
      try {
        response = await fetch(parsed, { signal: controller.signal, redirect: 'follow' });
        if (!response.ok) continue;
        const declared = Number(response.headers.get('content-length') ?? 0);
        if (declared && declared > MAX_THUMBNAIL_BYTES) continue;
        buffer = Buffer.from(await response.arrayBuffer());
      } finally {
        clearTimeout(timer);
      }

      if (!buffer || buffer.length === 0 || buffer.length > MAX_THUMBNAIL_BYTES || !isJpeg(buffer)) continue;

      const target = path.join(targetDir, 'thumb.jpg');
      fs.writeFileSync(target, buffer);
      return target;
    } catch (error) {
      log?.debug('Thumbnail candidate failed', { message: error.message });
    }
  }

  return null;
}
