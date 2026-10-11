/**
 * yt-dlp fallback downloader.
 *
 * TypeTube is the primary engine; this exists so a user still gets their file when
 * TypeTube is rate limited, its cluster rejects a regional track, or the runtime is
 * too old for TypeTube. It runs as a child process so the job timeout and the Cancel
 * button can actually kill it.
 */

import { spawn } from 'node:child_process';
import path from 'node:path';
import { largestFileWithExtension } from './workspace.js';
import { heightOf } from './validate.js';

export class DownloadFailedError extends Error {
  constructor(message, { source = 'yt-dlp', stderr = '', code = 'DOWNLOAD_FAILED' } = {}) {
    super(message);
    this.name = 'DownloadFailedError';
    this.source = source;
    this.stderr = stderr;
    this.code = code;
  }
}

/** Video format ladder: prefer mp4/m4a so the Bot API accepts the result as a video. */
export function videoFormatSelector(quality) {
  const height = heightOf(quality) || 1080;
  return (
    `bv*[height<=${height}][ext=mp4]+ba[ext=m4a]/` +
    `bv*[height<=${height}]+ba/` +
    `b[height<=${height}]/b`
  );
}

export function audioFormatSelector() {
  return 'ba[ext=m4a]/ba/b';
}

/** Parses one `--newline` progress line from yt-dlp. */
export function parseProgressLine(line) {
  const percent = /\[download\]\s+([\d.]+)%/.exec(line);
  if (!percent) return null;
  const speed = /at\s+([\d.]+\w?i?B\/s)/.exec(line);
  const eta = /ETA\s+([\d:]+)/.exec(line);
  const total = /of\s+~?\s*([\d.]+\w?i?B)/.exec(line);
  return {
    percent: Math.min(100, Number(percent[1])),
    speed: speed?.[1] ?? null,
    eta: eta?.[1] ?? null,
    total: total?.[1] ?? null
  };
}

/** True when yt-dlp cannot help (bad URL, private video, …) — no point retrying. */
export function isPermanentFailure(stderr = '') {
  return /Private video|Video unavailable|This video is not available|Sign in to confirm|members-only|has been removed|Unsupported URL|HTTP Error 404|is not a valid URL/i.test(
    stderr
  );
}

/** Human-readable cause extracted from yt-dlp's stderr. */
export function describeFailure(stderr = '') {
  if (/Private video/i.test(stderr)) return 'This video is private.';
  if (/members-only|Join this channel/i.test(stderr)) return 'This video is for channel members only.';
  if (/Sign in to confirm|bot/i.test(stderr)) return 'YouTube asked this server to sign in (bot check). Try again later or use another host.';
  if (/Video unavailable|not available in your country|blocked/i.test(stderr)) return 'This video is unavailable or blocked in the worker region.';
  if (/has been removed|HTTP Error 404/i.test(stderr)) return 'The video was removed or the link is wrong.';
  if (/age/i.test(stderr)) return 'This video is age restricted, so it cannot be downloaded anonymously.';
  if (/ffmpeg/i.test(stderr)) return 'FFmpeg is missing on the worker, so audio and video could not be merged.';
  if (/No space left/i.test(stderr)) return 'The worker ran out of disk space.';
  return null;
}

/**
 * Runs yt-dlp into `targetDir`.
 *
 * @param {object} options
 * @param {string} options.url
 * @param {string} options.quality
 * @param {'video'|'audio'} options.mode
 * @param {string} options.targetDir
 * @param {number} options.timeoutMs
 * @param {AbortSignal} options.signal
 * @param {(progress: {percent: number, speed: string|null, eta: string|null}) => void} [options.onProgress]
 * @param {(line: string) => void} [options.onLog]
 * @param {string} [options.binary] yt-dlp executable override
 * @returns {Promise<{ filePath: string, source: 'yt-dlp' }>}
 */
export async function downloadWithYtDlp({
  url,
  quality,
  mode,
  targetDir,
  timeoutMs,
  signal,
  onProgress,
  onLog,
  binary = process.env.YTDLP_BINARY || 'yt-dlp'
}) {
  const template = path.join(targetDir, '%(title).80B [%(id)s].%(ext)s');

  const args = [
    '--no-playlist',
    '--no-warnings',
    '--newline',
    '--restrict-filenames',
    '--no-call-home',
    '--socket-timeout', '30',
    '--retries', '3',
    '--fragment-retries', '3',
    '--concurrent-fragments', '2',
    '-o', template
  ];

  if (mode === 'audio') {
    args.push('-f', audioFormatSelector(), '-x', '--audio-format', 'm4a', '--audio-quality', '0');
  } else {
    args.push('-f', videoFormatSelector(quality), '--merge-output-format', 'mp4');
  }

  args.push('--', url);

  if (signal?.aborted) {
    throw new DownloadFailedError('Download cancelled before it started.', { source: 'yt-dlp', code: 'CANCELLED' });
  }

  const stderrChunks = [];

  await new Promise((resolve, reject) => {
    const child = spawn(binary, args, { stdio: ['ignore', 'pipe', 'pipe'], env: { ...process.env } });
    let settled = false;

    const finish = (fn, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      signal?.removeEventListener('abort', onAbort);
      fn(value);
    };

    const onAbort = () => {
      child.kill('SIGKILL');
      finish(reject, new DownloadFailedError('Download cancelled.', { source: 'yt-dlp', code: 'CANCELLED' }));
    };

    const timer = setTimeout(() => {
      child.kill('SIGKILL');
      finish(
        reject,
        new DownloadFailedError(`yt-dlp exceeded the ${Math.round(timeoutMs / 1000)}s limit.`, {
          source: 'yt-dlp',
          stderr: stderrChunks.join(''),
          code: 'TIMEOUT'
        })
      );
    }, timeoutMs);

    signal?.addEventListener('abort', onAbort, { once: true });

    child.stdout.on('data', (buffer) => {
      for (const line of buffer.toString().split(/\r?\n/)) {
        const progress = parseProgressLine(line);
        if (progress) onProgress?.(progress);
      }
    });

    child.stderr.on('data', (buffer) => {
      const text = buffer.toString();
      stderrChunks.push(text);
      if (stderrChunks.length > 200) stderrChunks.shift();
      for (const line of text.split(/\r?\n/)) {
        if (line.trim()) onLog?.(line.trim());
      }
    });

    child.on('error', (error) => {
      finish(
        reject,
        new DownloadFailedError(
          error.code === 'ENOENT'
            ? 'yt-dlp is not installed on the worker image.'
            : `Could not start yt-dlp: ${error.message}`,
          { source: 'yt-dlp', code: error.code === 'ENOENT' ? 'YTDLP_MISSING' : 'SPAWN_FAILED' }
        )
      );
    });

    child.on('close', (code) => {
      if (settled) return;
      if (code === 0) {
        finish(resolve, undefined);
        return;
      }
      const stderr = stderrChunks.join('');
      finish(
        reject,
        new DownloadFailedError(describeFailure(stderr) ?? `yt-dlp exited with code ${code}.`, {
          source: 'yt-dlp',
          stderr,
          code: 'YTDLP_FAILED'
        })
      );
    });
  });

  const found = await largestFileWithExtension(targetDir, ['.mp4', '.m4a', '.mkv', '.webm', '.mov']);
  if (!found) {
    throw new DownloadFailedError('yt-dlp finished but produced no usable file.', { source: 'yt-dlp' });
  }

  return { filePath: found.path, source: 'yt-dlp' };
}
