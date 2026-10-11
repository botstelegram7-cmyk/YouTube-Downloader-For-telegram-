/**
 * End-to-end tests for the Serverless handlers, run against the local SDK stub.
 *
 * These exercise the real handler modules (tgcloud/handlers/*.js) — the same files
 * the platform loads — including the database round-trips, the worker HTTP calls and
 * the exact copy the user sees.
 */

import test from 'node:test';
import assert from 'node:assert/strict';

import { api, fetch, jsonResponse, resetStub, __internals } from './stubs/sdk.js';
import handleMessage from '../tgcloud/handlers/message.js';
import handleCallbackQuery from '../tgcloud/handlers/callback_query.js';
import { CONFIG, SETTING_KEYS } from '../tgcloud/lib/config.js';
import { deleteSetting, getSetting, setSetting } from '../tgcloud/lib/storage.js';
import { botSettings, downloadJobs, userPreferences } from '../tgcloud/schema.js';

const WORKER_URL = 'https://worker.example';
const WORKER_SECRET = 's'.repeat(40);

const rowsOf = (table) => __internals.ROWS.get(table.__name) ?? [];

function message(overrides = {}) {
  return {
    message_id: 10,
    chat: { id: 500, type: 'private' },
    from: { id: 900, first_name: 'Test' },
    text: '/start',
    ...overrides
  };
}

function callback(overrides = {}) {
  return {
    id: 'cb-1',
    from: { id: 900 },
    message: { message_id: 77, chat: { id: 500 } },
    data: 'help',
    ...overrides
  };
}

/** The text of the last message the bot sent. */
function lastText() {
  const calls = api.__callsOf('sendMessage');
  return calls.length > 0 ? calls[calls.length - 1].params.text : null;
}

function allText() {
  return [...api.__callsOf('sendMessage'), ...api.__callsOf('editMessageText')].map((call) => call.params.text ?? '').join('\n');
}

function healthyWorker({ position = 0, running = true } = {}) {
  fetch.__setHandler(async (url) => {
    if (url.endsWith('/download')) return jsonResponse(202, { ok: true, job_id: 'worker-job', position, running });
    if (url.endsWith('/health')) {
      return jsonResponse(200, {
        ok: true,
        version: '2.0.0',
        uptimeSeconds: 120,
        queue: { running: 0, pending: 0, concurrency: 1 },
        runtime: { node: 'v24.9.0', ffmpeg: '/usr/bin/ffmpeg', memoryMb: 120, freeDiskMb: 8000 },
        typeTube: { supported: true, zstd: true }
      });
    }
    if (url.includes('/cancel')) return jsonResponse(200, { ok: true, status: 'cancelled' });
    return jsonResponse(404, { ok: false, error: 'not found' });
  });
}

async function configureWorker() {
  await setSetting(SETTING_KEYS.WORKER_URL, WORKER_URL);
  await setSetting(SETTING_KEYS.WORKER_SECRET, WORKER_SECRET);
}

test.beforeEach(() => resetStub());

/* ------------------------------------------------------------------ /start  */

test('/start introduces the bot and repeats the legal note', async () => {
  await handleMessage(message());
  const text = lastText();
  assert.match(text, /YouTube Downloader/);
  assert.match(text, /TypeTube/);
  assert.match(text, /Only download videos you own/);
});

test('commands with a @botname suffix still work', async () => {
  await handleMessage(message({ text: '/start@PyTuber_bot' }));
  assert.match(lastText(), /YouTube Downloader/);
});

test('non-text messages are ignored', async () => {
  await handleMessage(message({ text: undefined, photo: [{ file_id: 'x' }] }));
  assert.equal(api.__calls.length, 0);
});

test('a message without a chat id is ignored', async () => {
  await handleMessage({ text: '/start' });
  assert.equal(api.__calls.length, 0);
});

/* ------------------------------------------------------------------- /help  */

test('/help lists commands, settings and the 50 MB limit', async () => {
  await handleMessage(message({ text: '/quality 1080p' }));
  await handleMessage(message({ text: '/help' }));
  const text = lastText();

  assert.match(text, /How to use this bot/);
  assert.match(text, /\/quality/);
  assert.match(text, /360p, 480p, 720p, 1080p, 1440p, 2160p/);
  assert.match(text, /50 MB/);
  assert.match(text, /1080p · video/, 'shows the saved preference');
  assert.match(text, /not configured yet/);
});

test('/help hides owner commands from regular users', async () => {
  await setSetting(SETTING_KEYS.OWNER_USER_ID, '1');
  await handleMessage(message({ text: '/help' }));
  assert.ok(!lastText().includes('/setup'));
});

/* ---------------------------------------------------------------- /quality  */

test('/quality without arguments shows tappable buttons', async () => {
  await handleMessage(message({ text: '/quality' }));
  const call = api.__callsOf('sendMessage').at(-1);
  const buttons = call.params.reply_markup.inline_keyboard.flat();

  assert.ok(buttons.some((button) => button.callback_data === 'q:720p'));
  assert.ok(buttons.some((button) => button.text.includes('✅ 720p')), 'marks the current quality');
  assert.ok(buttons.every((button) => button.callback_data.length <= 64));
});

test('/quality saves a valid choice and rejects the rest', async () => {
  await handleMessage(message({ text: '/quality 480p' }));
  assert.match(lastText(), /480p/);
  assert.equal(rowsOf(userPreferences)[0].quality, '480p');

  await handleMessage(message({ text: '/quality 4k' }));
  assert.match(lastText(), /Usage/);
  assert.equal(rowsOf(userPreferences)[0].quality, '480p');
});

test('quality is case-insensitive', async () => {
  await handleMessage(message({ text: '/quality 1080P' }));
  assert.equal(rowsOf(userPreferences)[0].quality, '1080p');
});

/* ------------------------------------------------------------------- modes  */

test('/audio and /video switch the delivery mode', async () => {
  await handleMessage(message({ text: '/audio' }));
  assert.match(lastText(), /Audio mode on/);
  assert.equal(rowsOf(userPreferences)[0].mode, 'audio');

  await handleMessage(message({ text: '/video' }));
  assert.match(lastText(), /Video mode on/);
  assert.equal(rowsOf(userPreferences)[0].mode, 'video');
});

test('/mode validates its argument', async () => {
  await handleMessage(message({ text: '/mode gif' }));
  assert.match(lastText(), /Usage/);

  await handleMessage(message({ text: '/mode audio' }));
  assert.equal(rowsOf(userPreferences)[0].mode, 'audio');
});

/* ------------------------------------------------------------ bad messages  */

test('an unknown command and a non-link both get a helpful reply', async () => {
  await handleMessage(message({ text: '/frobnicate' }));
  assert.match(lastText(), /do not know/);

  await handleMessage(message({ text: 'hello there' }));
  assert.match(lastText(), /does not look like a YouTube video link/);
});

/* -------------------------------------------------------------- downloads  */

test('a link with no worker configured explains how to fix it', async () => {
  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));
  assert.match(lastText(), /not connected to a download worker/);
  assert.match(lastText(), /\/setup/);
});

test('a link submits a job and shows a cancel button', async () => {
  await configureWorker();
  healthyWorker();

  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));
  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));
  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));

  assert.equal(fetch.__callsTo('/download').length, 1, 'exactly one submission');

  const body = JSON.parse(fetch.__callsTo('/download')[0].options.body);
  assert.equal(body.url, 'https://youtu.be/jNQXAC9IVRw');
  assert.equal(body.quality, '720p');
  assert.equal(body.mode, 'video');
  assert.equal(body.chat_id, 500);
  assert.equal(body.user_id, 900);
  assert.ok(body.job_id.startsWith('j'));
  assert.equal(typeof body.status_message_id, 'number');

  assert.equal(rowsOf(downloadJobs).length, 1);
  assert.equal(rowsOf(downloadJobs)[0].status, 'queued');

  const edit = api.__callsOf('editMessageText').at(-1);
  assert.match(edit.params.text, /Starting now|Queued at position/);
  assert.match(edit.params.reply_markup.inline_keyboard[0][0].callback_data, /^cancel:j/);
});

test('the quality sent to the worker is the saved preference', async () => {
  await configureWorker();
  healthyWorker();

  await handleMessage(message({ text: '/quality 360p' }));
  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));

  const body = JSON.parse(fetch.__callsTo('/download')[0].options.body);
  assert.equal(body.quality, '360p');
});

test('/download 480p <url> overrides the saved quality once', async () => {
  await configureWorker();
  healthyWorker();

  await handleMessage(message({ text: '/download 480p https://youtu.be/jNQXAC9IVRw' }));
  const body = JSON.parse(fetch.__callsTo('/download')[0].options.body);
  assert.equal(body.quality, '480p');

  const preference = await getSetting(SETTING_KEYS.WORKER_URL);
  assert.ok(preference, 'worker config untouched');
});

test('/download without a URL explains the syntax', async () => {
  await handleMessage(message({ text: '/download 480p' }));
  assert.match(lastText(), /Usage/);
});

test('a wrong shared secret produces actionable advice', async () => {
  await configureWorker();
  fetch.__setHandler(async () => jsonResponse(401, { ok: false, error: 'unauthorized' }));

  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));

  const text = allText();
  assert.match(text, /rejected the shared secret/);
  assert.match(text, /WORKER_SECRET/);
  assert.equal(rowsOf(downloadJobs)[0].status, 'failed_auth');
});

test('a sleeping worker is reported honestly instead of as a generic error', async () => {
  await configureWorker();
  fetch.__setHandler(async () => {
    throw new Error('socket hang up');
  });

  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));

  const text = allText();
  assert.match(text, /did not answer in time/);
  assert.match(text, /15 minutes/);
  assert.equal(rowsOf(downloadJobs)[0].status, 'failed_submit');
});

test('a busy worker is reported as busy', async () => {
  await configureWorker();
  fetch.__setHandler(async () => jsonResponse(503, { ok: false, error: 'worker is busy' }));

  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));
  assert.match(allText(), /busy right now|finishing other downloads/);
});

test('a second submission while one is running is refused before hitting the worker', async () => {
  await configureWorker();
  healthyWorker();

  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));
  await handleMessage(message({ text: 'https://youtu.be/dQw4w9WgXcQ' }));

  assert.equal(fetch.__callsTo('/download').length, 1);
  assert.match(lastText(), /already have a download running/);
});

test('the daily cap eventually stops a heavy user', async () => {
  await configureWorker();
  healthyWorker();

  for (let index = 0; index < CONFIG.MAX_JOBS_PER_DAY; index += 1) {
    rowsOf(downloadJobs).push({
      id: `seed-${index}`,
      userId: 900,
      chatId: 500,
      status: 'done',
      createdAt: new Date(Date.now() - (index + 2) * 60_000)
    });
  }

  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));
  assert.match(lastText(), /reached today's limit/);
  assert.equal(fetch.__callsTo('/download').length, 0);
});

/* ----------------------------------------------------------------- /setup  */

test('/setup stores the credentials, deletes the message and masks the secret', async () => {
  healthyWorker();

  await handleMessage(message({ text: `/setup ${WORKER_URL} ${WORKER_SECRET}` }));

  assert.equal(await getSetting(SETTING_KEYS.WORKER_URL), WORKER_URL);
  assert.equal(await getSetting(SETTING_KEYS.WORKER_SECRET), WORKER_SECRET);
  assert.equal(await getSetting(SETTING_KEYS.OWNER_USER_ID), '900', 'first /setup claims ownership');

  const deletion = api.__callsOf('deleteMessage');
  assert.equal(deletion.length, 1);
  assert.equal(deletion[0].params.chat_id, 500);

  const text = lastText();
  assert.ok(!text.includes(WORKER_SECRET), 'the secret must never be echoed in full');
  assert.match(text, /Secret stored \(ssss…ssss\)/);
  assert.match(text, /Worker answered/);
});

test('/setup rejects bad input without writing anything', async () => {
  await handleMessage(message({ text: '/setup' }));
  assert.match(lastText(), /Usage/);

  await handleMessage(message({ text: '/setup http://worker.example supersecretsupersecret' }));
  assert.match(lastText(), /does not look like a worker URL/);

  await handleMessage(message({ text: `https://youtu.be/x` }));
  await handleMessage(message({ text: '/setup https://worker.example short' }));
  assert.match(lastText(), /too short/);

  assert.equal(await getSetting(SETTING_KEYS.WORKER_URL), null);
});

test('/setup keeps the message when Telegram refuses to delete it', async () => {
  healthyWorker();
  api.__failOnce('deleteMessage', { code: 400, description: 'Bad Request: message cannot be deleted' });

  await handleMessage(message({ text: `/setup ${WORKER_URL} ${WORKER_SECRET}` }));
  assert.equal(await getSetting(SETTING_KEYS.WORKER_SECRET), WORKER_SECRET);
});

test('owner-only commands refuse everyone else', async () => {
  await setSetting(SETTING_KEYS.OWNER_USER_ID, '1');

  await handleMessage(message({ text: `/setup ${WORKER_URL} ${WORKER_SECRET}` }));
  assert.match(lastText(), /only available to the bot owner/);

  await handleMessage(message({ text: '/stats' }));
  assert.match(lastText(), /only available to the bot owner/);

  await handleMessage(message({ text: '/unset' }));
  assert.match(lastText(), /only available to the bot owner/);

  assert.equal(await getSetting(SETTING_KEYS.WORKER_URL), null);
});

test('/config shows the connection state with a masked secret', async () => {
  await configureWorker();
  await handleMessage(message({ text: '/config' }));

  const text = lastText();
  assert.match(text, /Configuration/);
  assert.match(text, /worker\.example/);
  assert.match(text, /ssss…ssss/);
  assert.ok(!text.includes(WORKER_SECRET));
});

test('/unset forgets the worker', async () => {
  await configureWorker();
  await handleMessage(message({ text: '/unset' }));

  assert.match(lastText(), /credentials removed/);
  assert.equal(await getSetting(SETTING_KEYS.WORKER_URL), null);
  assert.equal(await getSetting(SETTING_KEYS.WORKER_SECRET), null);
});

/* ----------------------------------------------------------------- /status  */

test('/status reports the worker when it is awake', async () => {
  await configureWorker();
  healthyWorker();

  await handleMessage(message({ text: '/status' }));
  const text = lastText();
  assert.match(text, /online/);
  assert.match(text, /v24\.9\.0/);
  assert.match(text, /FFmpeg/);
  assert.match(text, /TypeTube: <b>available<\/b>/);
});

test('/status stays graceful while the worker sleeps', async () => {
  await configureWorker();
  fetch.__setHandler(async () => {
    throw new Error('offline');
  });

  await handleMessage(message({ text: '/status' }));
  assert.match(lastText(), /unreachable/);
});

test('/cancel without a running job says so', async () => {
  await configureWorker();
  await handleMessage(message({ text: '/cancel' }));
  assert.match(lastText(), /no download running/);
});

test('/stats needs a worker and reports queue details', async () => {
  await handleMessage(message({ text: '/stats' }));
  assert.match(lastText(), /not connected to a download worker/);

  await configureWorker();
  healthyWorker();
  await handleMessage(message({ text: '/stats' }));

  const text = lastText();
  assert.match(text, /Worker stats/);
  assert.match(text, /Running: 0/);
  assert.match(text, /zstd: yes/);
});

/* --------------------------------------------------------------- buttons   */

test('the quality button saves the choice and edits the message', async () => {
  await handleCallbackQuery(callback({ data: 'q:1440p' }));

  assert.equal(rowsOf(userPreferences)[0].quality, '1440p');
  assert.equal(api.__callsOf('answerCallbackQuery').length, 1);
  assert.match(api.__callsOf('editMessageText').at(-1).params.text, /1440p/);
});

test('the cancel button stops a running job', async () => {
  await configureWorker();
  healthyWorker();
  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));

  const job = rowsOf(downloadJobs)[0];
  await handleCallbackQuery(callback({ data: `cancel:${job.id}` }));

  assert.equal(fetch.__callsTo('/cancel').length, 1);
  assert.equal(rowsOf(downloadJobs)[0].status, 'cancelled');
  assert.equal(api.__callsOf('answerCallbackQuery').length, 1);
});

test('the retry button re-submits the stored link at the smaller quality', async () => {
  await configureWorker();
  healthyWorker();
  await handleMessage(message({ text: 'https://youtu.be/jNQXAC9IVRw' }));

  const job = rowsOf(downloadJobs)[0];
  fetch.__reset();
  healthyWorker();

  await handleCallbackQuery(callback({ data: `dl:480p:${job.id}` }));

  const submissions = fetch.__callsTo('/download');
  assert.equal(submissions.length, 1);
  const body = JSON.parse(submissions[0].options.body);
  assert.equal(body.quality, '480p');
  assert.equal(body.url, 'https://youtu.be/jNQXAC9IVRw');
});

test('a retry button cannot be used on someone else’s job', async () => {
  await configureWorker();
  await setSetting(SETTING_KEYS.WORKER_URL, WORKER_URL);
  rowsOf(downloadJobs).push({
    id: 'someone-else',
    userId: 1,
    chatId: 500,
    sourceUrl: 'https://youtu.be/jNQXAC9IVRw',
    quality: '1080p',
    mode: 'video',
    status: 'done',
    createdAt: new Date()
  });

  await handleCallbackQuery(callback({ data: 'dl:480p:someone-else' }));
  assert.equal(fetch.__callsTo('/download').length, 0);
  assert.match(api.__callsOf('answerCallbackQuery').at(-1).params.text, /Only the person/);
});

test('a missing job row is reported instead of crashing', async () => {
  await handleCallbackQuery(callback({ data: 'dl:480p:gone' }));
  assert.match(api.__callsOf('answerCallbackQuery').at(-1).params.text, /no longer in the database/);
});

test('unknown or malformed button data is answered, never ignored', async () => {
  await handleCallbackQuery(callback({ data: 'nonsense' }));
  assert.equal(api.__callsOf('answerCallbackQuery').length, 1);

  await handleCallbackQuery(callback({ data: 'q:8k' }));
  assert.equal(api.__callsOf('answerCallbackQuery').length, 2);
});

test('the help button sends the help text', async () => {
  await handleCallbackQuery(callback({ data: 'help' }));
  assert.match(lastText(), /How to use this bot/);
});

test('every callback is acknowledged even when the API call fails', async () => {
  api.__failOnce('answerCallbackQuery', { code: 400, description: 'query is too old' });
  await assert.doesNotReject(() => handleCallbackQuery(callback({ data: 'help' })));
});

test('a callback without an id is ignored', async () => {
  await handleCallbackQuery({ data: 'help' });
  assert.equal(api.__calls.length, 0);
});

/* --------------------------------------------------------------- storage   */

test('settings survive a rewrite and can be deleted', async () => {
  await setSetting('worker_url', 'https://a.example');
  await setSetting('worker_url', 'https://b.example');
  assert.equal(await getSetting('worker_url'), 'https://b.example');
  assert.equal(rowsOf(botSettings).length, 1, 'update, not duplicate insert');

  await deleteSetting('worker_url');
  assert.equal(await getSetting('worker_url'), null);
});
