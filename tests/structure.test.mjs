/**
 * The Serverless project layout rules, asserted as tests.
 *
 * These are the mistakes that waste the most time, because they only surface at
 * deploy time (or worse, at run time inside Telegram's sandbox).
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import path from 'node:path';
import { readFileSync, existsSync } from 'node:fs';
import { fileURLToPath } from 'node:url';

import { checkModules, readSpecifiers } from '../scripts/check_modules.mjs';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const result = checkModules(root);

test('the tgcloud/ layout satisfies every platform rule', () => {
  assert.deepEqual(result.failures, [], result.failures.join('\n'));
  assert.ok(result.checked >= 7, `expected the modules to be found, saw ${result.checked}`);
});

test('the required modules exist', () => {
  for (const relative of [
    'tgcloud/schema.js',
    'tgcloud/handlers/message.js',
    'tgcloud/handlers/callback_query.js',
    'tgcloud/lib/config.js',
    'tgcloud/lib/youtube.js',
    'tgcloud/lib/messages.js',
    'tgcloud/lib/storage.js',
    'tgcloud/lib/worker_api.js',
    'tgcloud/lib/download_flow.js'
  ]) {
    assert.ok(existsSync(path.join(root, relative)), `${relative} is missing`);
  }
});

test('handlers use the documented export shape', () => {
  for (const relative of ['tgcloud/handlers/message.js', 'tgcloud/handlers/callback_query.js']) {
    const source = readFileSync(path.join(root, relative), 'utf8');
    assert.match(source, /export\s+default\s+async\s+function/, `${relative} must export a default async function`);
  }
});

test('no module under tgcloud/ touches Node builtins or npm packages', () => {
  const { files } = result;
  assert.ok(files.length > 0);

  for (const relative of files) {
    const source = readFileSync(path.join(root, relative), 'utf8');
    for (const specifier of readSpecifiers(source)) {
      assert.ok(!specifier.startsWith('node:'), `${relative} imports ${specifier}`);
      assert.ok(
        specifier.startsWith('.') || specifier === 'sdk' || specifier.startsWith('sdk/'),
        `${relative} imports ${specifier}`
      );
    }
  }
});

test('the schema avoids the constructs the platform rejects', () => {
  const source = readFileSync(path.join(root, 'tgcloud/schema.js'), 'utf8');
  assert.ok(!/\.references\(/.test(source), 'foreign keys throw when declared');
  assert.ok(!/foreignKey\(/.test(source), 'foreign keys throw when declared');
});

test('callback_data stays inside Telegram’s 64-byte limit', () => {
  const source = readFileSync(path.join(root, 'tgcloud/lib/youtube.js'), 'utf8');
  assert.match(source, /slice\(0, 64\)/);
});

test('the config module holds no credentials', () => {
  const source = readFileSync(path.join(root, 'tgcloud/lib/config.js'), 'utf8');
  const configBlock = /export const CONFIG[\s\S]*?\n\}\);/.exec(source)?.[0] ?? '';
  assert.ok(configBlock.includes('WORKER_URL'), 'CONFIG block not found');

  for (const [, key, value] of configBlock.matchAll(/^\s*(WORKER_URL|WORKER_SECRET)\s*:\s*'([^']*)'/gm)) {
    assert.equal(value, '', `${key} must stay empty in the committed config`);
  }
});

test('the worker and the bot agree on the HTTP contract', () => {
  const worker = readFileSync(path.join(root, 'worker/src/server.js'), 'utf8');
  const client = readFileSync(path.join(root, 'tgcloud/lib/worker_api.js'), 'utf8');

  for (const route of ['/download', '/health']) {
    assert.ok(worker.includes(route), `worker does not serve ${route}`);
    assert.ok(client.includes(route), `client does not call ${route}`);
  }
  assert.ok(/jobs\\?\//.test(worker), 'worker does not route job actions');
  assert.ok(client.includes('/jobs/'), 'client does not call the job routes');
  assert.ok(worker.includes('cancel') && client.includes('cancel'), 'cancel is not wired on both sides');

  for (const field of ['job_id', 'chat_id', 'user_id', 'status_message_id', 'quality', 'mode', 'url']) {
    assert.ok(worker.includes(field), `worker does not read ${field}`);
    assert.ok(client.includes(field), `client does not send ${field}`);
  }
});
