#!/usr/bin/env node
/**
 * End-to-end verification without a real bot.
 *
 * Starts a fake Telegram Bot API, boots the worker against it, submits a real job and
 * reports what actually happened — including the size of the file that was "uploaded".
 * This is how you check a change to the download or upload path before deploying.
 *
 *   node scripts/e2e_local.mjs                       # defaults to a 20-second test video
 *   node scripts/e2e_local.mjs <youtube-url> 480p audio
 *
 * It needs the real dependencies (TypeTube, FFmpeg for muxing) and outbound network
 * access, so run it on a machine where you would also run the worker itself.
 */

import http from 'node:http';
import { spawn } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const [, , urlArg = 'https://youtu.be/jNQXAC9IVRw', qualityArg = '360p', modeArg = 'video'] = process.argv;

const BOT_TOKEN = '123456789:E2E-ONLY-NOT-A-REAL-TOKEN-aaaaaaaaaa';
const WORKER_SECRET = 'e2e-only-shared-secret-0123456789abcdef';
const CHAT_ID = 424242;
const STATUS_MESSAGE_ID = 99;

const received = { edits: [], uploads: [], deletions: 0, chatActions: 0 };

/** Minimal but faithful Bot API surface for the methods the worker uses. */
function startFakeTelegram() {
  const server = http.createServer((request, response) => {
    const method = request.url.split('/').pop();

    const chunks = [];
    request.on('data', (chunk) => chunks.push(chunk));
    request.on('end', () => {
      const raw = Buffer.concat(chunks);

      const json = (payload) => {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify(payload));
      };

      if (method === 'getMe') {
        return json({ ok: true, result: { id: 1, is_bot: true, username: 'e2e_bot', first_name: 'E2E' } });
      }

      if (method === 'editMessageText') {
        const body = JSON.parse(raw.toString('utf8'));
        received.edits.push(body.text);
        return json({ ok: true, result: { message_id: body.message_id, text: body.text } });
      }

      if (method === 'sendChatAction') {
        received.chatActions += 1;
        return json({ ok: true, result: true });
      }

      if (method === 'deleteMessage') {
        received.deletions += 1;
        return json({ ok: true, result: true });
      }

      if (method === 'sendVideo' || method === 'sendAudio' || method === 'sendDocument') {
        // Only the size and a couple of markers are inspected, not the whole payload.
        const text = raw.toString('latin1');
        const caption = /name="caption"\r\n\r\n([\s\S]*?)\r\n/.exec(text)?.[1] ?? '';
        received.uploads.push({
          method,
          bytes: raw.length,
          hasThumbnail: text.includes('name="thumbnail"'),
          caption
        });
        return json({ ok: true, result: { message_id: 1234, chat: { id: CHAT_ID } } });
      }

      return json({ ok: false, error_code: 404, description: `unknown method ${method}` });
    });
  });

  return new Promise((resolve) => {
    server.listen(0, '127.0.0.1', () => resolve({ server, port: server.address().port }));
  });
}

async function waitForHealth(port, timeoutMs = 40_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const response = await fetch(`http://127.0.0.1:${port}/health`);
      if (response.ok) return await response.json();
    } catch {
      // still starting
    }
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error('worker did not become healthy in time');
}

async function waitForJob(port, timeoutMs = 15 * 60 * 1000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const response = await fetch(`http://127.0.0.1:${port}/stats`, {
      headers: { authorization: `Bearer ${WORKER_SECRET}` }
    });
    const body = await response.json();
    if (body.queue.running === 0 && body.queue.pending === 0) return body;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error('job did not finish in time');
}

const { server: telegramServer, port: telegramPort } = await startFakeTelegram();
const workerPort = 8123;

console.log(`fake Telegram API on :${telegramPort}, worker on :${workerPort}`);

const worker = spawn(process.execPath, [path.join(root, 'src/index.js')], {
  env: {
    ...process.env,
    BOT_TOKEN,
    WORKER_SECRET,
    PORT: String(workerPort),
    TELEGRAM_API_BASE: `http://127.0.0.1:${telegramPort}`,
    MAX_CONCURRENT_DOWNLOADS: '1',
    DOWNLOAD_WORKERS: '4',
    LOG_LEVEL: 'info'
  },
  stdio: ['ignore', 'pipe', 'pipe']
});

worker.stdout.on('data', (buffer) => process.stdout.write(`[worker] ${buffer}`));
worker.stderr.on('data', (buffer) => process.stderr.write(`[worker] ${buffer}`));

let exitCode = 0;

try {
  const health = await waitForHealth(workerPort);
  console.log('\n--- health -------------------------------------------------------');
  console.log(JSON.stringify(health, null, 2));

  if (!health.typeTube?.supported) {
    console.warn('\n! TypeTube is unavailable on this runtime; the yt-dlp fallback will be used.');
  }

  const submit = await fetch(`http://127.0.0.1:${workerPort}/download`, {
    method: 'POST',
    headers: { 'content-type': 'application/json', authorization: `Bearer ${WORKER_SECRET}` },
    body: JSON.stringify({
      job_id: 'e2e-job-1',
      url: urlArg,
      chat_id: CHAT_ID,
      user_id: 7,
      status_message_id: STATUS_MESSAGE_ID,
      quality: qualityArg,
      mode: modeArg
    })
  });

  console.log(`\n--- POST /download → HTTP ${submit.status}`);
  console.log(JSON.stringify(await submit.json()));

  if (submit.status !== 202) throw new Error(`expected HTTP 202, received ${submit.status}`);

  await waitForJob(workerPort);

  console.log('\n--- status edits seen by Telegram --------------------------------');
  for (const text of received.edits) console.log(` • ${text.replace(/\n/g, ' | ')}`);
  console.log(`\n--- uploads -----------------------------------------------------`);
  for (const upload of received.uploads) {
    console.log(
      ` • ${upload.method}: ${(upload.bytes / 1_000_000).toFixed(2)} MB multipart` +
        (upload.hasThumbnail ? ' (with thumbnail)' : '')
    );
    if (upload.caption) console.log(`   caption: ${upload.caption.replace(/\n/g, ' | ')}`);
  }
  console.log(` • status message deletions: ${received.deletions}`);
  console.log(` • typing/upload chat actions: ${received.chatActions}`);

  const delivered = received.uploads.some((upload) => ['sendVideo', 'sendAudio', 'sendDocument'].includes(upload.method));
  const lastEdit = received.edits.at(-1) ?? '';

  if (delivered && received.deletions > 0) {
    console.log('\n✅ PASS — the file was downloaded, muxed and delivered.');
  } else if (delivered) {
    console.log('\n⚠️ PARTIAL — the file was delivered but the status message was not cleaned up.');
    exitCode = 1;
  } else {
    console.log(`\n❌ FAIL — nothing was uploaded. Last status: ${lastEdit}`);
    exitCode = 1;
  }
} catch (error) {
  console.error(`\n❌ ${error.message}`);
  exitCode = 1;
} finally {
  worker.kill('SIGTERM');
  telegramServer.close();
  // Give the worker a moment to shut down cleanly before the process exits.
  await new Promise((resolve) => setTimeout(resolve, 500));
  process.exit(exitCode);
}
