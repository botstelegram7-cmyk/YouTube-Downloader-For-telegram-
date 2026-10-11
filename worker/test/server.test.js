import test from 'node:test';
import assert from 'node:assert/strict';
import http from 'node:http';

import { createQueue, createRequestHandler, extractBearer, parseJobRequest, secretMatches } from '../src/server.js';
import { loadConfig } from '../src/config.js';
import { Logger } from '../src/logger.js';

const SECRET = 'unit-test-secret-value-0123456789abcdef';

function testConfig(overrides = {}) {
  const { config } = loadConfig({
    BOT_TOKEN: '123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
    WORKER_SECRET: SECRET,
    LOG_LEVEL: 'error',
    ...overrides
  });
  return config;
}

async function withServer(run, { config = testConfig(), processJob = async () => 'done' } = {}) {
  const logs = [];
  const log = {
    child: () => log,
    debug: (m, f) => logs.push([m, f]),
    info: (m, f) => logs.push([m, f]),
    warn: (m, f) => logs.push([m, f]),
    error: (m, f) => logs.push([m, f])
  };
  const queue = createQueue({ config, log, processJob });
  const server = http.createServer(createRequestHandler({ config, log, queue }));

  await new Promise((resolve) => server.listen(0, '127.0.0.1', resolve));
  const base = `http://127.0.0.1:${server.address().port}`;

  try {
    return await run({ base, queue, logs, config });
  } finally {
    queue.cancelAll('test finished');
    await new Promise((resolve) => server.close(resolve));
  }
}

const authHeaders = (secret = SECRET) => ({
  'content-type': 'application/json',
  authorization: `Bearer ${secret}`
});

const post = (base, path, body, headers = authHeaders()) =>
  fetch(`${base}${path}`, { method: 'POST', headers, body: JSON.stringify(body) });

const validJob = (overrides = {}) => ({
  url: 'https://youtu.be/jNQXAC9IVRw',
  chat_id: 12345,
  user_id: 67890,
  status_message_id: 99,
  quality: '720p',
  mode: 'video',
  ...overrides
});

test('secretMatches compares in constant time and rejects everything else', () => {
  assert.equal(secretMatches(SECRET, SECRET), true);
  assert.equal(secretMatches(SECRET, `${SECRET}x`), false);
  assert.equal(secretMatches(SECRET, SECRET.slice(0, -1)), false);
  assert.equal(secretMatches(SECRET, ''), false);
  assert.equal(secretMatches('', SECRET), false);
  assert.equal(secretMatches(SECRET, undefined), false);
});

test('extractBearer only accepts the Bearer scheme', () => {
  assert.equal(extractBearer('Bearer abc'), 'abc');
  assert.equal(extractBearer('bearer   abc  '), 'abc');
  assert.equal(extractBearer('abc'), '');
  assert.equal(extractBearer(undefined), '');
  assert.equal(extractBearer('Basic abc'), '');
});

test('parseJobRequest normalises and validates payloads', () => {
  const parsed = parseJobRequest(validJob());
  assert.equal(parsed.url, 'https://youtu.be/jNQXAC9IVRw');
  assert.equal(parsed.chatId, 12345);
  assert.equal(parsed.quality, '720p');
  assert.equal(parsed.mode, 'video');

  assert.throws(() => parseJobRequest(validJob({ url: 'https://evil.example/x' })), /url must be/);
  assert.throws(() => parseJobRequest(validJob({ quality: '4k' })), /quality must be/);
  assert.throws(() => parseJobRequest(validJob({ mode: 'gif' })), /mode must be/);
  assert.throws(() => parseJobRequest(validJob({ chat_id: 'abc' })), /chat_id must be/);
  assert.throws(() => parseJobRequest(validJob({ status_message_id: 0 })), /status_message_id must be/);
  assert.throws(() => parseJobRequest({}), /Invalid request/);
});

test('GET /health needs no secret and exposes no credentials', async () => {
  await withServer(async ({ base }) => {
    const response = await fetch(`${base}/health`);
    assert.equal(response.status, 200);
    const body = await response.json();

    assert.equal(body.ok, true);
    assert.equal(body.service, 'yt-telegram-download-worker');
    assert.ok(body.runtime.node.startsWith('v'));
    assert.ok('typeTube' in body);

    const raw = JSON.stringify(body);
    assert.ok(!raw.includes(SECRET), 'health must not leak the shared secret');
    assert.ok(!raw.includes('123456789:'), 'health must not leak the bot token');
  });
});

test('protected routes reject a missing or wrong secret', async () => {
  await withServer(async ({ base }) => {
    const anonymous = await fetch(`${base}/stats`);
    assert.equal(anonymous.status, 401);

    const wrong = await fetch(`${base}/stats`, { headers: authHeaders('wrong-secret-wrong-secret') });
    assert.equal(wrong.status, 401);

    const empty = await post(base, '/download', validJob(), { 'content-type': 'application/json' });
    assert.equal(empty.status, 401);
  });
});

test('POST /download accepts a valid job and reports the queue position', async () => {
  await withServer(async ({ base }) => {
    const response = await post(base, '/download', validJob());
    assert.equal(response.status, 202);
    const body = await response.json();
    assert.equal(body.ok, true);
    assert.equal(body.running, true);
    assert.match(body.job_id, /^[A-Za-z0-9_-]{6,64}$/);
  });
});

test('POST /download rejects hostile payloads with 400 and a reason', async () => {
  await withServer(async ({ base }) => {
    for (const payload of [
      validJob({ url: 'https://youtube.com.evil.example/watch?v=x' }),
      validJob({ url: 'file:///etc/passwd' }),
      validJob({ quality: '4320p' })
    ]) {
      const response = await post(base, '/download', payload);
      assert.equal(response.status, 400);
      const body = await response.json();
      assert.equal(body.ok, false);
      assert.ok(Array.isArray(body.problems) && body.problems.length > 0);
    }
  });
});

test('POST /download rejects malformed JSON and oversized bodies', async () => {
  await withServer(async ({ base }) => {
    const malformed = await fetch(`${base}/download`, {
      method: 'POST',
      headers: authHeaders(),
      body: '{not json'
    });
    assert.equal(malformed.status, 400);

    const huge = await fetch(`${base}/download`, {
      method: 'POST',
      headers: authHeaders(),
      body: JSON.stringify({ pad: 'x'.repeat(20_000) })
    });
    assert.equal(huge.status, 413);
  });
});

test('a second job for the same chat is refused with DUPLICATE', async () => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });

  await withServer(
    async ({ base }) => {
      const first = await post(base, '/download', validJob());
      assert.equal(first.status, 202);

      const second = await post(base, '/download', validJob());
      assert.equal(second.status, 409);
      const body = await second.json();
      assert.equal(body.code, 'DUPLICATE');

      release();
    },
    { processJob: async () => gate }
  );
});

test('a chat can cancel its running job through the API', async () => {
  let release;
  const gate = new Promise((resolve) => {
    release = resolve;
  });

  await withServer(
    async ({ base }) => {
      const created = await (await post(base, '/download', validJob())).json();
      assert.equal(created.job_id.length > 0, true);

      const cancelled = await post(base, `/jobs/${created.job_id}/cancel`, {});
      assert.equal(cancelled.status, 200);
      assert.equal((await cancelled.json()).status, 'cancelled');

      // Cancelling twice is idempotent: the Cancel button may be pressed repeatedly.
      const again = await post(base, `/jobs/${created.job_id}/cancel`, {});
      assert.equal(again.status, 200);
      assert.equal((await again.json()).status, 'cancelled');

      const unknown = await post(base, '/jobs/does-not-exist/cancel', {});
      assert.equal(unknown.status, 404);

      release();
    },
    { processJob: async () => gate }
  );
});

test('unknown routes and wrong methods return JSON errors', async () => {
  await withServer(async ({ base }) => {
    const missing = await fetch(`${base}/nope`, { headers: authHeaders() });
    assert.equal(missing.status, 404);
    assert.equal((await missing.json()).ok, false);

    const wrongMethod = await fetch(`${base}/download`, { headers: authHeaders() });
    assert.equal(wrongMethod.status, 404);
  });
});

test('the service banner is available at / without a secret', async () => {
  await withServer(async ({ base }) => {
    const response = await fetch(`${base}/`);
    assert.equal(response.status, 200);
    assert.equal((await response.json()).ok, true);
  });
});

test('configured concurrency limits how many jobs run at once', async () => {
  const started = [];
  await withServer(
    async ({ base, queue }) => {
      await post(base, '/download', validJob({ chat_id: 1, user_id: 1 }));
      await post(base, '/download', validJob({ chat_id: 2, user_id: 2 }));
      await new Promise((resolve) => setTimeout(resolve, 20));
      assert.equal(started.length, 1, 'only one job should start with MAX_CONCURRENT_DOWNLOADS=1');
      assert.equal(queue.pending, 1);
    },
    {
      config: testConfig({ MAX_CONCURRENT_DOWNLOADS: '1' }),
      processJob: async () => {
        started.push(Date.now());
        await new Promise((resolve) => setTimeout(resolve, 30));
      }
    }
  );
});

test('the logger used by requests never receives the secret', async () => {
  await withServer(async ({ base, logs }) => {
    await fetch(`${base}/stats`, { headers: authHeaders() });
    await post(base, '/download', validJob({ url: 'nonsense' }));
    assert.ok(!JSON.stringify(logs).includes(SECRET));
  });
});

test('Logger.child keeps working after redaction (smoke test)', () => {
  const logger = new Logger({ level: 'error', base: { service: 'test' } });
  assert.doesNotThrow(() => logger.child({ jobId: 'x' }).error('boom', { token: 'secret', message: 'fine' }));
});
