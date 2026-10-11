/**
 * Client for the Render download worker.
 *
 * The pure helpers at the top are exported separately so they can be unit-tested in
 * plain Node, where the Serverless `sdk` module does not exist (see tests/worker_api.test.mjs).
 *
 * Why the retry loop matters: on Render's free plan the worker sleeps after 15 minutes
 * of inactivity and needs roughly 50 seconds to wake up. The worker answers a
 * submission in milliseconds once it is awake, so we retry the POST with a short
 * timeout a few times and, if it still fails, tell the user the truth ("the worker is
 * waking up, send the link again") instead of a generic error.
 */

import { fetch } from 'sdk';

export class WorkerError extends Error {
  constructor(kind, message, { status = 0, body = null } = {}) {
    super(message);
    this.name = 'WorkerError';
    this.kind = kind;
    this.status = status;
    this.body = body;
  }
}

/** Rejects when `promise` takes too long; the caller must not leave the promise unhandled. */
export function withTimeout(promise, timeoutMs, onTimeout) {
  return new Promise((resolve, reject) => {
    let settled = false;
    const timer = setTimeout(() => {
      if (settled) return;
      settled = true;
      onTimeout?.();
      reject(new Error(`Timed out after ${timeoutMs} ms`));
    }, timeoutMs);

    promise.then(
      (value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        reject(error);
      }
    );
  });
}

/** Turns an HTTP failure into a category the user-facing copy understands. */
export function classifyStatus(status, body) {
  if (status === 401 || status === 403) return new WorkerError('unauthorized', 'Worker rejected the shared secret', { status, body });
  if (status === 409) return new WorkerError('duplicate', 'Worker already has a job for this chat', { status, body });
  if (status === 429 || status === 503) return new WorkerError('busy', 'Worker is at capacity', { status, body });
  if (status === 413) return new WorkerError('badRequest', 'Request body too large', { status, body });
  if (status >= 400 && status < 500) return new WorkerError('badRequest', body?.error ?? `Worker returned ${status}`, { status, body });
  return new WorkerError('server', body?.error ?? `Worker returned ${status}`, { status, body });
}

export function classifyNetworkError(error) {
  return new WorkerError('unreachable', error?.message ?? 'Network error', {});
}

export function buildDownloadPayload({ jobId, url, chatId, userId, statusMessageId, quality, mode }) {
  return {
    job_id: jobId,
    url,
    chat_id: chatId,
    user_id: userId,
    status_message_id: statusMessageId,
    quality,
    mode
  };
}

/** `abcd1234…cdef` — enough to recognise a secret without showing it. */
export function maskSecret(secret) {
  if (typeof secret !== 'string' || secret.length === 0) return '';
  if (secret.length <= 8) return '*'.repeat(secret.length);
  return `${secret.slice(0, 4)}…${secret.slice(-4)}`;
}

export function normalizeBaseUrl(value) {
  if (typeof value !== 'string') return '';
  const trimmed = value.trim().replace(/\/+$/, '');
  if (!trimmed) return '';
  // Only add a scheme when none was given: "ftp://x" and "javascript:..." must keep
  // their own scheme so isValidWorkerUrl can reject them.
  if (/^[a-z][a-z\d+.-]*:/i.test(trimmed)) return trimmed;
  return `https://${trimmed}`;
}

/** Public worker URL validation: https on Render, or http for localhost during testing. */
export function isValidWorkerUrl(value) {
  const normalized = normalizeBaseUrl(value);
  try {
    const parsed = new URL(normalized);
    if (parsed.protocol === 'https:') return true;
    return parsed.protocol === 'http:' && ['localhost', '127.0.0.1'].includes(parsed.hostname);
  } catch {
    return false;
  }
}

/** Backoff for attempt `index` (0-based), clamped to the configured list. */
export function backoffFor(index, list = [1_500, 4_000]) {
  if (list.length === 0) return 0;
  return list[Math.min(index, list.length - 1)];
}

async function postJson(url, secret, payload, timeoutMs) {
  const result = await withTimeout(
    fetch(url, {
      method: 'POST',
      headers: {
        'content-type': 'application/json',
        authorization: `Bearer ${secret}`
      },
      body: JSON.stringify(payload)
    }),
    timeoutMs
  );

  let body = null;
  try {
    body = await result.json();
  } catch {
    body = null;
  }

  if (!result.ok) throw classifyStatus(result.status, body);
  return body;
}

/**
 * Submits a job, retrying the way a cold Render service needs.
 * @returns {Promise<{job_id: string, position: number, running: boolean}>}
 */
export async function submitJob({ workerUrl, workerSecret, payload, config }) {
  const base = `${normalizeBaseUrl(workerUrl)}/download`;
  let lastError = null;

  for (let attempt = 0; attempt < config.SUBMIT_ATTEMPTS; attempt += 1) {
    try {
      return await postJson(base, workerSecret, payload, config.SUBMIT_TIMEOUT_MS);
    } catch (error) {
      lastError = error instanceof WorkerError ? error : classifyNetworkError(error);
      // A rejected payload or a wrong secret will never succeed on retry.
      if (lastError.kind === 'unauthorized' || lastError.kind === 'badRequest') throw lastError;

      const wait = backoffFor(attempt, config.SUBMIT_BACKOFF_MS);
      if (attempt < config.SUBMIT_ATTEMPTS - 1 && wait > 0) {
        await new Promise((resolve) => setTimeout(resolve, wait));
      }
    }
  }

  throw lastError ?? new WorkerError('unreachable', 'Worker did not respond');
}

/** Asks the worker to cancel a job. Best effort: the status message is the source of truth. */
export async function cancelJob({ workerUrl, workerSecret, jobId, timeoutMs = 10_000 }) {
  const url = `${normalizeBaseUrl(workerUrl)}/jobs/${encodeURIComponent(jobId)}/cancel`;
  try {
    const response = await withTimeout(
      fetch(url, { method: 'POST', headers: { authorization: `Bearer ${workerSecret}`, 'content-type': 'application/json' }, body: '{}' }),
      timeoutMs
    );
    return response.ok;
  } catch (error) {
    console.log('cancelJob failed', { jobId, message: error?.message });
    return false;
  }
}

/** Reads /health. Returns null instead of throwing: /status must work while the worker sleeps. */
export async function fetchHealth({ workerUrl, timeoutMs = 8_000 }) {
  try {
    const response = await withTimeout(fetch(`${normalizeBaseUrl(workerUrl)}/health`, { method: 'GET' }), timeoutMs);
    if (!response.ok) return null;
    return await response.json();
  } catch {
    return null;
  }
}
