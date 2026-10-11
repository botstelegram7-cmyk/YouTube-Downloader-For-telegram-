/**
 * Worker client: the pure helpers, plus submit/retry behaviour driven through the
 * stubbed `fetch`.
 *
 * The retry loop is the part worth testing: on Render's free plan the worker sleeps
 * after 15 minutes, so the first POST often fails and a later one succeeds.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { fetch, jsonResponse, resetStub } from './stubs/sdk.js';
import {
  WorkerError,
  backoffFor,
  buildDownloadPayload,
  cancelJob,
  classifyNetworkError,
  classifyStatus,
  fetchHealth,
  isValidWorkerUrl,
  maskSecret,
  normalizeBaseUrl,
  submitJob,
  withTimeout
} from '../tgcloud/lib/worker_api.js';
import { CONFIG } from '../tgcloud/lib/config.js';

const fastConfig = { ...CONFIG, SUBMIT_ATTEMPTS: 3, SUBMIT_TIMEOUT_MS: 50, SUBMIT_BACKOFF_MS: [0, 0] };

test.beforeEach(() => resetStub());

test('base URL normalisation adds https and strips trailing slashes', () => {
  assert.equal(normalizeBaseUrl('worker.onrender.com/'), 'https://worker.onrender.com');
  assert.equal(normalizeBaseUrl('https://worker.onrender.com//'), 'https://worker.onrender.com');
  assert.equal(normalizeBaseUrl('  https://a.example  '), 'https://a.example');
  assert.equal(normalizeBaseUrl(undefined), '');
});

test('only https (or localhost for development) is accepted as a worker URL', () => {
  assert.equal(isValidWorkerUrl('https://worker.onrender.com'), true);
  assert.equal(isValidWorkerUrl('worker.onrender.com'), true);
  assert.equal(isValidWorkerUrl('http://localhost:8080'), true);
  assert.equal(isValidWorkerUrl('http://worker.onrender.com'), false);
  assert.equal(isValidWorkerUrl('ftp://worker.example'), false);
  assert.equal(isValidWorkerUrl('not a url'), false);
});

test('secrets are masked to four characters on each side', () => {
  assert.equal(maskSecret('abcdefghijklmnop'), 'abcd…mnop');
  assert.equal(maskSecret('short'), '*****');
  assert.equal(maskSecret(''), '');
  assert.equal(maskSecret(undefined), '');
});

test('HTTP failures map onto the user-facing categories', () => {
  assert.equal(classifyStatus(401, null).kind, 'unauthorized');
  assert.equal(classifyStatus(403, null).kind, 'unauthorized');
  assert.equal(classifyStatus(409, null).kind, 'duplicate');
  assert.equal(classifyStatus(503, null).kind, 'busy');
  assert.equal(classifyStatus(429, null).kind, 'busy');
  assert.equal(classifyStatus(400, { error: 'bad' }).kind, 'badRequest');
  assert.equal(classifyStatus(500, null).kind, 'server');
  assert.equal(classifyNetworkError(new Error('ECONNRESET')).kind, 'unreachable');
});

test('payload shape matches what the worker validates', () => {
  const payload = buildDownloadPayload({
    jobId: 'j1',
    url: 'https://youtu.be/jNQXAC9IVRw',
    chatId: 5,
    userId: 6,
    statusMessageId: 7,
    quality: '720p',
    mode: 'video'
  });
  assert.deepEqual(payload, {
    job_id: 'j1',
    url: 'https://youtu.be/jNQXAC9IVRw',
    chat_id: 5,
    user_id: 6,
    status_message_id: 7,
    quality: '720p',
    mode: 'video'
  });
});

test('backoff never reads past the configured list', () => {
  assert.equal(backoffFor(0, [1_500, 4_000]), 1_500);
  assert.equal(backoffFor(1, [1_500, 4_000]), 4_000);
  assert.equal(backoffFor(9, [1_500, 4_000]), 4_000);
  assert.equal(backoffFor(0, []), 0);
});

test('withTimeout rejects a promise that never settles', async () => {
  await assert.rejects(() => withTimeout(new Promise(() => {}), 20), /Timed out/);
  assert.equal(await withTimeout(Promise.resolve('ok'), 200), 'ok');
});

test('submitJob retries a sleeping worker and then succeeds', async () => {
  let attempts = 0;
  fetch.__setHandler(async () => {
    attempts += 1;
    if (attempts === 1) throw new Error('socket hang up');
    return jsonResponse(202, { ok: true, job_id: 'j1', position: 0, running: true });
  });

  const result = await submitJob({
    workerUrl: 'https://worker.example',
    workerSecret: 's'.repeat(32),
    payload: buildDownloadPayload({
      jobId: 'j1',
      url: 'https://youtu.be/jNQXAC9IVRw',
      chatId: 1,
      userId: 1,
      statusMessageId: 1,
      quality: '720p',
      mode: 'video'
    }),
    config: fastConfig
  });

  assert.equal(attempts, 2);
  assert.equal(result.position, 0);
  assert.equal(fetch.__calls[0].url, 'https://worker.example/download');
  assert.equal(fetch.__calls[0].options.headers.authorization, `Bearer ${'s'.repeat(32)}`);
});

test('submitJob sends the bearer secret and never retries a wrong secret', async () => {
  let attempts = 0;
  fetch.__setHandler(async () => {
    attempts += 1;
    return jsonResponse(401, { ok: false, error: 'unauthorized' });
  });

  await assert.rejects(
    () =>
      submitJob({
        workerUrl: 'https://worker.example',
        workerSecret: 'wrong',
        payload: {},
        config: fastConfig
      }),
    (error) => {
      assert.ok(error instanceof WorkerError);
      assert.equal(error.kind, 'unauthorized');
      return true;
    }
  );

  assert.equal(attempts, 1, 'a wrong secret must not be retried');
});

test('submitJob gives up after the configured attempts', async () => {
  let attempts = 0;
  fetch.__setHandler(async () => {
    attempts += 1;
    throw new Error('timed out');
  });

  await assert.rejects(
    () => submitJob({ workerUrl: 'https://worker.example', workerSecret: 'x', payload: {}, config: fastConfig }),
    (error) => {
      assert.equal(error.kind, 'unreachable');
      return true;
    }
  );

  assert.equal(attempts, fastConfig.SUBMIT_ATTEMPTS);
});

test('cancelJob reports success and swallows network failures', async () => {
  fetch.__setHandler(async () => jsonResponse(200, { ok: true, status: 'cancelled' }));
  assert.equal(await cancelJob({ workerUrl: 'https://worker.example', workerSecret: 'x', jobId: 'j1' }), true);
  assert.match(fetch.__calls[0].url, /\/jobs\/j1\/cancel$/);

  fetch.__setHandler(async () => {
    throw new Error('offline');
  });
  assert.equal(await cancelJob({ workerUrl: 'https://worker.example', workerSecret: 'x', jobId: 'j2' }), false);
});

test('fetchHealth returns null instead of throwing while the worker sleeps', async () => {
  fetch.__setHandler(async () => {
    throw new Error('503');
  });
  assert.equal(await fetchHealth({ workerUrl: 'https://worker.example' }), null);

  fetch.__setHandler(async () => jsonResponse(200, { ok: true, version: '2.0.0' }));
  const health = await fetchHealth({ workerUrl: 'https://worker.example' });
  assert.equal(health.version, '2.0.0');
});
