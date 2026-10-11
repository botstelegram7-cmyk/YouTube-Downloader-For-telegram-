/**
 * FFmpeg helpers.
 *
 * TypeTube muxes video+audio itself, but two cases still need FFmpeg directly:
 *  - an audio stream that is not MP4/AAC (Telegram only treats MP3/M4A as music);
 *  - yt-dlp's `-x --audio-format m4a` extraction.
 * `-c copy` is used wherever possible so a 0.1 vCPU instance stays affordable.
 */

import { spawn } from 'node:child_process';

let cachedAvailability = null;

/** Resolves to the ffmpeg binary name when usable, otherwise `null`. Cached. */
export async function ffmpegPath() {
  if (cachedAvailability !== null) return cachedAvailability;

  const binary = process.env.FFMPEG_BINARY || 'ffmpeg';
  cachedAvailability = await new Promise((resolve) => {
    try {
      const child = spawn(binary, ['-version'], { stdio: ['ignore', 'ignore', 'ignore'] });
      child.on('error', () => resolve(null));
      child.on('close', (code) => resolve(code === 0 ? binary : null));
    } catch {
      resolve(null);
    }
  });
  return cachedAvailability;
}

export async function isFfmpegAvailable() {
  return (await ffmpegPath()) !== null;
}

/** Runs ffmpeg, resolving with stderr tail on success and rejecting with it on failure. */
export async function runFfmpeg(args, { signal, timeoutMs = 300_000, log } = {}) {
  const binary = (await ffmpegPath()) ?? 'ffmpeg';

  if (signal?.aborted) throw new Error('FFmpeg cancelled before it started.');

  return await new Promise((resolve, reject) => {
    const child = spawn(binary, args, { stdio: ['ignore', 'ignore', 'pipe'] });
    let stderr = '';
    let settled = false;

    const done = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      fn(value);
    };

    const onAbort = () => {
      child.kill('SIGKILL');
      done(reject, new Error('FFmpeg cancelled.'));
    };

    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      done(reject, new Error(`FFmpeg exceeded the ${Math.round(timeoutMs / 1000)}s limit.`));
    }, timeoutMs);

    signal?.addEventListener('abort', onAbort, { once: true });

    child.stderr.on('data', (buffer) => {
      stderr += buffer.toString();
      if (stderr.length > 20_000) stderr = stderr.slice(-10_000);
    });

    child.on('error', (error) => {
      done(reject, error.code === 'ENOENT' ? new Error('FFmpeg is not installed on the worker.') : error);
    });

    child.on('close', (code) => {
      if (code === 0) {
        done(resolve, stderr);
        return;
      }
      const tail = stderr.trim().split('\n').slice(-3).join(' | ').slice(0, 400);
      log?.warn('FFmpeg failed', { code, tail });
      done(reject, new Error(`FFmpeg exited with code ${code}: ${tail}`));
    });
  });
}

/** True when the container already holds M4A/AAC audio that Telegram accepts as music. */
export function isM4aCompatible(mimeType = '') {
  const value = String(mimeType).toLowerCase();
  return value.includes('audio/mp4') || value.includes('audio/m4a') || value.includes('audio/aac');
}

/**
 * Converts any audio stream to M4A/AAC without touching video.
 * `-c copy` is attempted first (free); a transcode is the fallback for Opus/WebM.
 */
export async function toM4a(inputPath, outputPath, { signal, log } = {}) {
  try {
    await runFfmpeg(
      ['-y', '-hide_banner', '-loglevel', 'error', '-i', inputPath, '-vn', '-c:a', 'copy', '-movflags', '+faststart', outputPath],
      { signal, timeoutMs: 120_000, log }
    );
    return { outputPath, mode: 'remux' };
  } catch (error) {
    log?.info('Remux to M4A did not work, transcoding instead', { message: error.message });
    await runFfmpeg(
      [
        '-y', '-hide_banner', '-loglevel', 'error',
        '-i', inputPath,
        '-vn', '-c:a', 'aac', '-b:a', '128k',
        '-movflags', '+faststart',
        outputPath
      ],
      { signal, timeoutMs: 600_000, log }
    );
    return { outputPath, mode: 'transcode' };
  }
}

/** Reads the duration in seconds from ffmpeg's own output (no ffprobe needed). */
export function parseDurationFromFfmpeg(stderr = '') {
  const match = /Duration:\s*(\d+):(\d+):(\d+\.\d+)/.exec(stderr);
  if (!match) return null;
  return Number(match[1]) * 3600 + Number(match[2]) * 60 + Number(match[3]);
}
