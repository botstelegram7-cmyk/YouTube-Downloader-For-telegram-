/**
 * Download orchestration: TypeTube first, yt-dlp only when TypeTube cannot deliver.
 *
 * Responsibilities kept out of the HTTP layer and out of TypeTube itself:
 *  - a per-attempt timeout built on Promise.race, because TypeTube has no AbortSignal;
 *  - an honest, actionable error for every failure mode;
 *  - automatic quality downgrade when the file would exceed the Bot API upload cap,
 *    which is the single most common complaint about 50 MB limits;
 *  - normalising the result (container, codecs, title, duration, dimensions) so the
 *    uploader does not have to care which engine produced the file.
 */

import path from 'node:path';
import fs from 'node:fs';
import { promises as fsp } from 'node:fs';
import { nextLowerQuality, safeFileName } from './validate.js';
import { downloadWithYtDlp } from './ytdlp.js';
import { closeTypeTubeClient, getTypeTubeClient } from './typetube.js';
import { isM4aCompatible, isFfmpegAvailable, ffmpegPath, toM4a } from './ffmpeg.js';
import { largestFileWithExtension } from './workspace.js';

export class CancelledError extends Error {
  constructor(message = 'Download cancelled.') {
    super(message);
    this.name = 'CancelledError';
    this.code = 'CANCELLED';
  }
}

export class TooLargeError extends Error {
  constructor(bytes, maxBytes, { quality, tried = [] } = {}) {
    super(
      `The result is ${formatMb(bytes)}, above the ${formatMb(maxBytes)} upload limit` +
        (tried.length ? ` (tried ${tried.join(', ')})` : '') +
        '.'
    );
    this.name = 'TooLargeError';
    this.code = 'TOO_LARGE';
    this.bytes = bytes;
    this.maxBytes = maxBytes;
    this.quality = quality;
    this.tried = tried;
  }
}

export function formatMb(bytes) {
  return `${(bytes / 1_000_000).toFixed(1)} MB`;
}

/** Rejects as soon as `signal` aborts, so a race can end early. */
function abortable(signal) {
  if (!signal) return new Promise(() => {});
  if (signal.aborted) return Promise.reject(new CancelledError());
  return new Promise((_resolve, reject) => {
    signal.addEventListener('abort', () => reject(new CancelledError()), { once: true });
  });
}

async function withTimeout(promise, timeoutMs, label, { signal, onTimeout } = {}) {
  let timer;
  const timeout = new Promise((_resolve, reject) => {
    timer = setTimeout(() => {
      onTimeout?.();
      const error = new Error(`${label} timed out after ${Math.round(timeoutMs / 1000)}s.`);
      error.code = 'TIMEOUT';
      reject(error);
    }, timeoutMs);
  });

  try {
    return await Promise.race([promise, timeout, abortable(signal)]);
  } finally {
    clearTimeout(timer);
  }
}

export class Downloader {
  /**
   * @param {object} options
   * @param {object} options.config Parsed worker configuration.
   * @param {import('./logger.js').Logger} options.log
   */
  constructor({ config, log }) {
    this.config = config;
    this.log = log;
  }

  /**
   * Downloads one job.
   *
   * @param {object} job
   * @param {string} job.url
   * @param {string} job.quality
   * @param {'video'|'audio'} job.mode
   * @param {string} job.dir
   * @param {AbortSignal} job.signal
   * @param {(update: {percent?: number, text?: string, phase?: string}) => void} [job.onProgress]
   * @returns {Promise<object>} normalised download result
   */
  async download({ url, quality, mode, dir, signal, onProgress }) {
    const tried = [];
    let currentQuality = quality;

    // First attempt at the requested quality, then progressively lower when the
    // result does not fit inside the Telegram upload limit.
    for (let attempt = 0; attempt <= this.config.MAX_DOWNGRADE_ATTEMPTS; attempt += 1) {
      if (signal.aborted) throw new CancelledError();

      const result = await this.#attempt({ url, quality: currentQuality, mode, dir, signal, onProgress, attempt });

      const maxBytes = this.config.MAX_UPLOAD_BYTES;
      const downgradeRelevant = mode === 'video' || this.config.MAX_UPLOAD_BYTES > 0;

      if (result.sizeBytes <= maxBytes || !this.config.AUTO_DOWNGRADE || !downgradeRelevant || attempt === this.config.MAX_DOWNGRADE_ATTEMPTS) {
        if (result.sizeBytes > maxBytes) {
          throw new TooLargeError(result.sizeBytes, maxBytes, { quality: currentQuality, tried });
        }
        return { ...result, quality: currentQuality, downgradeAttempts: attempt };
      }

      const lower = nextLowerQuality(currentQuality);
      tried.push(currentQuality);
      if (!lower) {
        throw new TooLargeError(result.sizeBytes, maxBytes, { quality: currentQuality, tried });
      }

      this.log.info('Result above the upload limit, retrying smaller', {
        quality: currentQuality,
        sizeBytes: result.sizeBytes,
        nextQuality: lower
      });
      onProgress?.({
        phase: 'downgrade',
        text: `📉 ${formatMb(result.sizeBytes)} is over the Telegram limit — retrying at ${lower}…`
      });

      await this.#cleanupArtifacts(dir);
      currentQuality = lower;
    }

    throw new TooLargeError(0, this.config.MAX_UPLOAD_BYTES, { quality, tried });
  }

  /** One full attempt: TypeTube first, then yt-dlp. */
  async #attempt({ url, quality, mode, dir, signal, onProgress, attempt }) {
    let typeTubeError = null;

    if (!signal.aborted) {
      try {
        onProgress?.({ phase: 'resolving', text: '🔎 Resolving streams with TypeTube…' });
        return await this.#downloadWithTypeTube({ url, quality, mode, dir, signal, onProgress });
      } catch (error) {
        if (error instanceof CancelledError) throw error;
        typeTubeError = error;
        this.log.warn('TypeTube attempt failed', { attempt, quality, mode, message: error.message, code: error.code });
      }
    }

    if (!this.config.YTDLP_FALLBACK) {
      throw typeTubeError ?? new Error('Download failed and the yt-dlp fallback is disabled.');
    }

    // Fresh directory so half-written TypeTube files cannot be mistaken for results.
    const fallbackDir = path.join(dir, `fallback-${attempt}`);
    await fsp.mkdir(fallbackDir, { recursive: true });

    onProgress?.({
      phase: 'fallback',
      text: typeTubeError ? '⚠️ TypeTube could not deliver this video — using the yt-dlp fallback…' : '⚙️ Using yt-dlp…'
    });

    const { filePath } = await downloadWithYtDlp({
      url,
      quality,
      mode,
      targetDir: fallbackDir,
      timeoutMs: Math.min(this.config.YTDLP_TIMEOUT_MS, this.config.JOB_TIMEOUT_MS),
      signal,
      onProgress: (progress) => {
        const parts = [`⬇️ Downloading with yt-dlp… ${progress.percent.toFixed(1)}%`];
        if (progress.speed) parts.push(progress.speed);
        if (progress.eta) parts.push(`ETA ${progress.eta}`);
        onProgress?.({ phase: 'downloading', percent: progress.percent, text: parts.join(' · ') });
      },
      onLog: (line) => this.log.debug('yt-dlp', { line })
    });

    const stat = await fsp.stat(filePath);
    return {
      filePath,
      sizeBytes: stat.size,
      source: 'yt-dlp',
      mode,
      title: path.basename(filePath, path.extname(filePath)),
      durationSeconds: 0,
      width: 0,
      height: 0,
      hasAudio: true,
      thumbnailUrl: null,
      formatLabel: quality
    };
  }

  async #downloadWithTypeTube({ url, quality, mode, dir, signal, onProgress }) {
    const client = await getTypeTubeClient(this.config);

    const resolveStarted = Date.now();
    const track = await withTimeout(
      client.resolve(url, { maxVideoHeight: Number.parseInt(quality, 10) || undefined }),
      this.config.RESOLVE_TIMEOUT_MS,
      'TypeTube resolve',
      {
        signal,
        onTimeout: () => {
          // The client is unusable after an abandoned call; drop it so the next job
          // starts from a clean socket instead of inheriting a stuck request.
          closeTypeTubeClient();
        }
      }
    );

    this.log.info('TypeTube resolved', {
      videoId: track.id,
      title: track.title,
      ms: Date.now() - resolveStarted,
      audioStreams: track.audioStreams?.length ?? 0,
      videoStreams: track.videoStreams?.length ?? 0
    });

    if (!track?.id) throw new Error('TypeTube returned an empty result for this link.');

    const progressHandler = (progress) => {
      if (!progress) return;
      const percent = Number.isFinite(progress.percent) ? progress.percent : undefined;
      if (progress.phase === 'muxing') {
        onProgress?.({ phase: 'muxing', text: '🎬 Merging video and audio with FFmpeg…' });
        return;
      }
      const parts = [`⬇️ Downloading with TypeTube… ${(percent ?? 0).toFixed(0)}%`];
      if (progress.speedMBps) parts.push(`${progress.speedMBps} MB/s`);
      onProgress?.({ phase: 'downloading', percent, text: parts.join(' · ') });
    };

    if (mode === 'audio') {
      return await this.#downloadAudioWithTypeTube({ client, track, dir, signal, onProgress: progressHandler });
    }

    return await this.#downloadVideoWithTypeTube({
      client,
      track,
      quality,
      dir,
      signal,
      onProgress: progressHandler
    });
  }

  async #downloadVideoWithTypeTube({ client, track, quality, dir, signal, onProgress }) {
    const outputPath = path.join(dir, 'media.mp4');
    const hasAudioStreams = Array.isArray(track.audioStreams) && track.audioStreams.length > 0;

    const run = async (withAudio) => {
      const timeoutDeadline = Math.min(this.config.JOB_TIMEOUT_MS, 900_000);
      if (withAudio) {
        // TypeTube defaults to whatever `ffmpeg` resolves to on PATH; pass our own
        // resolution so FFMPEG_BINARY and container images behave predictably.
        const binary = await ffmpegPath();
        return await withTimeout(
          client.downloadMedia(track, outputPath, {
            quality,
            outputFormat: 'mp4',
            preferCodec: 'avc1',
            workers: this.config.DOWNLOAD_WORKERS,
            chunkSizeBytes: this.config.CHUNK_SIZE_BYTES,
            ...(binary ? { ffmpegPath: binary } : {}),
            onProgress
          }),
          timeoutDeadline,
          'TypeTube download',
          { signal, onTimeout: () => closeTypeTubeClient() }
        );
      }
      return await withTimeout(
        client.downloadVideo(track, outputPath, {
          quality,
          preferCodec: 'avc1',
          workers: this.config.DOWNLOAD_WORKERS,
          chunkSizeBytes: this.config.CHUNK_SIZE_BYTES,
          onProgress
        }),
        timeoutDeadline,
        'TypeTube download',
        { signal, onTimeout: () => closeTypeTubeClient() }
      );
    };

    let result;
    let hasAudio = hasAudioStreams;

    try {
      result = hasAudioStreams
        ? await run(true)
        : await run(false);
    } catch (error) {
      // A missing FFmpeg only breaks muxing; the audio-less video is still useful.
      if (hasAudioStreams && /ffmpeg/i.test(String(error.message))) {
        this.log.warn('Muxing failed, delivering the video track only', { message: error.message });
        hasAudio = false;
        result = await run(false);
      } else {
        throw error;
      }
    }

    const filePath = result?.filePath && fs.existsSync(result.filePath) ? result.filePath : outputPath;
    if (!fs.existsSync(filePath)) {
      const found = await largestFileWithExtension(dir, ['.mp4', '.mkv', '.webm']);
      if (!found) throw new Error('TypeTube finished without producing a media file.');
      return {
        filePath: found.path,
        sizeBytes: found.size,
        source: 'typetube',
        mode: 'video',
        title: track.title,
        durationSeconds: track.durationSeconds ?? 0,
        width: 0,
        height: 0,
        hasAudio,
        thumbnailUrl: track.thumbnail ?? track.thumbnails?.[0]?.url ?? null,
        formatLabel: videoFormatLabel(result?.videoStream, quality)
      };
    }

    const stat = await fsp.stat(filePath);
    return {
      filePath,
      sizeBytes: stat.size,
      source: 'typetube',
      mode: 'video',
      title: track.title,
      durationSeconds: track.durationSeconds ?? 0,
      width: result?.width ?? 0,
      height: result?.height ?? 0,
      hasAudio,
      thumbnailUrl: track.thumbnail ?? track.thumbnails?.[0]?.url ?? null,
      formatLabel: videoFormatLabel(result?.videoStream, quality)
    };
  }

  async #downloadAudioWithTypeTube({ client, track, dir, signal, onProgress }) {
    if (!Array.isArray(track.audioStreams) || track.audioStreams.length === 0) {
      throw new Error('This video has no separate audio stream, so audio-only is not possible.');
    }

    const rawPath = path.join(dir, 'audio.raw');
    const result = await withTimeout(
      client.downloadAudio(track, rawPath, {
        audioQuality: this.config.TYPETUBE_API_KEY ? 'hifi' : 'highest',
        workers: this.config.DOWNLOAD_WORKERS,
        chunkSizeBytes: this.config.CHUNK_SIZE_BYTES,
        onProgress
      }),
      Math.min(this.config.JOB_TIMEOUT_MS, 900_000),
      'TypeTube audio download',
      { signal, onTimeout: () => closeTypeTubeClient() }
    );

    const downloaded = result?.filePath && fs.existsSync(result.filePath) ? result.filePath : rawPath;
    if (!fs.existsSync(downloaded)) throw new Error('TypeTube finished without producing an audio file.');

    const mimeType = result?.audioStream?.mimeType ?? '';
    const outputPath = path.join(dir, 'audio.m4a');
    let finalPath = downloaded;
    let converted = false;

    if (isM4aCompatible(mimeType)) {
      if (downloaded !== outputPath) await fsp.rename(downloaded, outputPath);
      finalPath = outputPath;
    } else if (await isFfmpegAvailable()) {
      onProgress?.({ phase: 'converting', text: '🎚️ Converting audio to M4A…' });
      await toM4a(downloaded, outputPath, { signal, log: this.log });
      finalPath = outputPath;
      converted = true;
    } else {
      // No FFmpeg: keep the original bytes and let the uploader send a document.
      const extension = /webm|opus/i.test(mimeType) ? '.opus' : '.audio';
      finalPath = `${path.join(dir, 'audio')}${extension}`;
      await fsp.rename(downloaded, finalPath);
    }

    const stat = await fsp.stat(finalPath);
    return {
      filePath: finalPath,
      sizeBytes: stat.size,
      source: 'typetube',
      mode: 'audio',
      title: track.title,
      performer: track.author,
      durationSeconds: track.durationSeconds ?? 0,
      width: 0,
      height: 0,
      hasAudio: true,
      containerIsMusic: finalPath.endsWith('.m4a'),
      converted,
      thumbnailUrl: track.thumbnail ?? track.thumbnails?.[0]?.url ?? null,
      formatLabel: `${result?.audioStream?.quality ?? 'audio'}${converted ? ' → AAC 128 kbps' : ''}`
    };
  }

  /** Deletes artefacts from the previous attempt so sizes are never mixed up. */
  async #cleanupArtifacts(dir) {
    try {
      const entries = await fsp.readdir(dir, { withFileTypes: true });
      for (const entry of entries) {
        if (!entry.isFile()) continue;
        if (/\.(mp4|m4a|webm|mkv|mov|opus|part|ytdl)$/i.test(entry.name)) {
          await fsp.rm(path.join(dir, entry.name), { force: true });
        }
      }
    } catch (error) {
      this.log.debug('Cleanup before retry failed', { message: error.message });
    }
  }
}

/** "1080p (avc1)" style label for captions. */
export function videoFormatLabel(videoStream, requestedQuality) {
  if (!videoStream) return requestedQuality;
  const codec = /avc1/i.test(videoStream.mimeType ?? '')
    ? 'H.264'
    : /vp9/i.test(videoStream.mimeType ?? '')
      ? 'VP9'
      : /av01/i.test(videoStream.mimeType ?? '')
        ? 'AV1'
        : null;
  const resolution = videoStream.resolution ?? videoStream.quality ?? requestedQuality;
  return codec ? `${resolution} (${codec})` : String(resolution);
}

export { safeFileName };
