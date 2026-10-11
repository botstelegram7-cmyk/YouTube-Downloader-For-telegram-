/**
 * HTTP surface of the worker.
 *
 *   GET  /              service banner (no secrets)
 *   GET  /health        liveness + capabilities, used by the Render health check
 *   GET  /stats         queue and runtime detail (authenticated)
 *   POST /download      queue a job              (authenticated)
 *   POST /jobs/:id/cancel  stop a job            (authenticated)
 *
 * The only caller is the Telegram Serverless handler, so authentication is a shared
 * bearer secret compared in constant time. Nothing else is exposed.
 */

import { timingSafeEqual } from 'node:crypto';
import { DuplicateJobError, JobQueue, QueueFullError } from './queue.js';
import { isMode, isQuality, normalizeYouTubeUrl } from './validate.js';
import { typeTubeRuntimeReport } from './typetube.js';
import { describeConfig } from './config.js';
import { freeSpaceBytes } from './workspace.js';
import { ffmpegPath } from './ffmpeg.js';

const MAX_BODY_BYTES = 16 * 1024;
const JOB_ID_PATTERN = /^[A-Za-z0-9_-]{6,64}$/;

export class AuthError extends Error {
  constructor() {
    super('Missing or invalid worker secret.');
    this.name = 'AuthError';
    this.code = 401;
  }
}

/** Constant-time comparison; length differences are normalised first. */
export function secretMatches(expected, supplied) {
  if (!expected || typeof supplied !== 'string' || supplied.length === 0) return false;
  const a = Buffer.from(expected);
  const b = Buffer.from(supplied);
  if (a.length !== b.length) {
    // Still do a comparison so timing does not reveal the length.
    timingSafeEqual(a, a);
    return false;
  }
  return timingSafeEqual(a, b);
}

export function extractBearer(headerValue) {
  if (typeof headerValue !== 'string') return '';
  const match = /^Bearer\s+(.+)$/i.exec(headerValue.trim());
  return match ? match[1].trim() : '';
}

async function readJsonBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > MAX_BODY_BYTES) {
      const error = new Error('Request body too large.');
      error.code = 413;
      throw error;
    }
    chunks.push(chunk);
  }
  const raw = Buffer.concat(chunks).toString('utf8').trim();
  if (!raw) return {};
  try {
    return JSON.parse(raw);
  } catch {
    const error = new Error('Request body is not valid JSON.');
    error.code = 400;
    throw error;
  }
}

function sendJson(response, status, payload) {
  const body = JSON.stringify(payload);
  response.writeHead(status, {
    'content-type': 'application/json; charset=utf-8',
    'content-length': Buffer.byteLength(body),
    'cache-control': 'no-store'
  });
  response.end(body);
}

/** Validates a /download payload and returns the normalised job request. */
export function parseJobRequest(payload) {
  const problems = [];

  const url = normalizeYouTubeUrl(payload?.url);
  if (!url) problems.push('url must be a YouTube video, Shorts or youtu.be link');

  const chatId = Number(payload?.chat_id);
  if (!Number.isInteger(chatId) || chatId === 0) problems.push('chat_id must be a non-zero integer');

  const userId = payload?.user_id === undefined || payload?.user_id === null ? 0 : Number(payload.user_id);
  if (payload?.user_id !== undefined && !Number.isInteger(userId)) problems.push('user_id must be an integer');

  const statusMessageId = Number(payload?.status_message_id);
  if (!Number.isInteger(statusMessageId) || statusMessageId <= 0) problems.push('status_message_id must be a positive integer');

  const quality = String(payload?.quality ?? '').toLowerCase();
  if (!isQuality(quality)) problems.push(`quality must be one of 360p, 480p, 720p, 1080p, 1440p, 2160p`);

  const mode = String(payload?.mode ?? 'video').toLowerCase();
  if (!isMode(mode)) problems.push('mode must be "video" or "audio"');

  const jobId = payload?.job_id === undefined ? null : String(payload.job_id);
  if (jobId !== null && !JOB_ID_PATTERN.test(jobId)) problems.push('job_id has an invalid shape');

  if (problems.length > 0) {
    const error = new Error(`Invalid request: ${problems.join('; ')}`);
    error.code = 400;
    error.problems = problems;
    throw error;
  }

  return { url, chatId, userId, statusMessageId, quality, mode, jobId };
}

/**
 * Builds the request handler. Kept dependency-injected so tests can drive the real
 * routing logic without a network or a Telegram token.
 */
export function createRequestHandler({ config, log, queue, startedAt = Date.now() }) {
  return async function handleRequest(request, response) {
    const url = new URL(request.url, 'http://worker.local');
    const pathname = url.pathname.replace(/\/+$/, '') || '/';
    const requestLog = log.child({ method: request.method, path: pathname });

    try {
      if (request.method === 'GET' && (pathname === '/' || pathname === '/health')) {
        const ffmpeg = await ffmpegPath();
        const typeTube = typeTubeRuntimeReport();
        const payload = {
          ok: true,
          service: 'yt-telegram-download-worker',
          version: config.VERSION,
          uptimeSeconds: Math.round((Date.now() - startedAt) / 1000),
          queue: { running: queue.running, pending: queue.pending, concurrency: queue.concurrency },
          runtime: {
            node: process.version,
            platform: `${process.platform}/${process.arch}`,
            ffmpeg: ffmpeg ?? 'missing',
            memoryMb: Math.round(process.memoryUsage().rss / 1_000_000),
            freeDiskMb: Math.round(((await freeSpaceBytes()) ?? 0) / 1_000_000)
          },
          typeTube: { supported: typeTube.supported, zstd: typeTube.zstd, note: typeTube.note },
          config: describeConfig(config)
        };
        sendJson(response, 200, pathname === '/' ? payload : { ok: true, ...payload });
        return;
      }

      // Everything below requires the shared secret.
      const secret = extractBearer(request.headers.authorization);
      if (!secretMatches(config.WORKER_SECRET, secret)) {
        requestLog.warn('Rejected unauthorised request');
        sendJson(response, 401, { ok: false, error: 'unauthorized' });
        return;
      }

      if (request.method === 'GET' && pathname === '/stats') {
        const typeTube = typeTubeRuntimeReport();
        sendJson(response, 200, {
          ok: true,
          queue: queue.stats(),
          typeTube,
          config: describeConfig(config),
          memory: process.memoryUsage(),
          uptimeSeconds: Math.round((Date.now() - startedAt) / 1000)
        });
        return;
      }

      if (request.method === 'POST' && pathname === '/download') {
        const payload = await readJsonBody(request);
        const jobRequest = parseJobRequest(payload);

        const { job, position } = queue.enqueue({
          id: jobRequest.jobId ?? `${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`,
          chatId: jobRequest.chatId,
          userId: jobRequest.userId,
          statusMessageId: jobRequest.statusMessageId,
          url: jobRequest.url,
          quality: jobRequest.quality,
          mode: jobRequest.mode,
          requestedAt: Date.now()
        });

        requestLog.info('Job accepted', { jobId: job.id, position, quality: job.quality, mode: job.mode });
        sendJson(response, 202, { ok: true, job_id: job.id, position, running: position === 0 });
        return;
      }

      const cancelMatch = /^\/jobs\/([A-Za-z0-9_-]{6,64})\/cancel$/.exec(pathname);
      if (request.method === 'POST' && cancelMatch) {
        const jobId = cancelMatch[1];
        const outcome = queue.cancel(jobId, 'cancelled by user');
        if (outcome === 'not_found') {
          sendJson(response, 404, { ok: false, error: 'no such job', job_id: jobId });
          return;
        }
        requestLog.info('Job cancelled through the API', { jobId });
        sendJson(response, 200, { ok: true, job_id: jobId, status: 'cancelled' });
        return;
      }

      requestLog.warn('Unknown route');
      sendJson(response, 404, { ok: false, error: 'not found' });
    } catch (error) {
      const status = Number(error?.status ?? error?.code) || 500;
      if (status >= 500) requestLog.error('Request failed', { message: error?.message, stack: error?.stack });
      else requestLog.info('Request rejected', { status, message: error?.message });

      const payload = { ok: false, error: error?.message ?? 'internal error' };
      if (Array.isArray(error?.problems)) payload.problems = error.problems;
      if (error instanceof QueueFullError) payload.code = 'QUEUE_FULL';
      if (error instanceof DuplicateJobError) payload.code = 'DUPLICATE';
      sendJson(response, status >= 400 && status < 600 ? status : 500, payload);
    }
  };
}

/** Creates the queue bound to the real job processor. */
export function createQueue({ config, log, processJob }) {
  return new JobQueue({
    concurrency: config.MAX_CONCURRENT_DOWNLOADS,
    maxWaiting: Math.max(10, config.MAX_CONCURRENT_DOWNLOADS * 10),
    log,
    run: (job) => processJob(job, { config, log })
  });
}
