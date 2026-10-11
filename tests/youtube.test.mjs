/**
 * URL/command rules for the Serverless copy.
 *
 * The fixtures are shared with the worker (`worker/test/fixtures/url_vectors.json`),
 * and worker/test/validate.test.js runs the same file against worker/src/validate.js.
 * That is what keeps the two implementations from drifting apart, which is the main
 * risk of duplicating logic across two runtimes.
 */

import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  QUALITIES,
  decodeCallbackData,
  encodeCallbackData,
  findYouTubeUrl,
  heightOf,
  isMode,
  isQuality,
  isSmallQuality,
  nextLowerQuality,
  normalizeYouTubeUrl,
  parseCommand,
  youtubeVideoId
} from '../tgcloud/lib/youtube.js';

const fixtures = JSON.parse(
  readFileSync(
    path.join(path.dirname(fileURLToPath(import.meta.url)), '..', 'worker/test/fixtures/url_vectors.json'),
    'utf8'
  )
);

test('accepts every URL shape from the shared fixture set', () => {
  for (const input of fixtures.accepted) {
    assert.ok(normalizeYouTubeUrl(input), `expected accepted: ${input}`);
  }
});

test('rejects every hostile URL from the shared fixture set', () => {
  for (const input of fixtures.rejected) {
    assert.equal(normalizeYouTubeUrl(input), null, `expected rejected: ${input}`);
  }
});

test('command parsing follows the shared fixture set', () => {
  for (const item of fixtures.commands) {
    assert.deepEqual(parseCommand(item.input), { command: item.command, args: item.args });
  }
});

test('video ids follow the shared fixture set', () => {
  for (const item of fixtures.videoIds) {
    assert.equal(youtubeVideoId(item.url), item.id);
  }
});

test('quality helpers mirror the worker', () => {
  assert.deepEqual(QUALITIES, ['360p', '480p', '720p', '1080p', '1440p', '2160p']);
  assert.equal(heightOf('720p'), 720);
  assert.equal(nextLowerQuality('1080p'), '720p');
  assert.equal(nextLowerQuality('360p'), null);
  assert.equal(isQuality('480p'), true);
  assert.equal(isQuality('480'), false);
  assert.equal(isMode('audio'), true);
  assert.equal(isSmallQuality('480p'), true);
  assert.equal(isSmallQuality('720p'), false);
});

test('callback data round-trips and is length limited', () => {
  assert.deepEqual(decodeCallbackData(encodeCallbackData(['q', '720p'])), { action: 'quality', quality: '720p' });
  assert.deepEqual(decodeCallbackData(encodeCallbackData(['cancel', 'jabc123'])), { action: 'cancel', jobId: 'jabc123' });
  assert.deepEqual(decodeCallbackData('dl:480p:jabc123'), { action: 'download', quality: '480p', jobId: 'jabc123' });
  assert.deepEqual(decodeCallbackData('help'), { action: 'help' });

  assert.equal(decodeCallbackData('q:4k'), null);
  assert.equal(decodeCallbackData('bogus'), null);
  assert.equal(decodeCallbackData(''), null);
  assert.equal(decodeCallbackData('x'.repeat(80)), null);
  assert.equal(decodeCallbackData(null), null);

  assert.ok(encodeCallbackData(['dl', '2160p', 'a'.repeat(60)]).length <= 64);
});

test('links are found inside normal sentences', () => {
  assert.equal(findYouTubeUrl('check this https://youtu.be/jNQXAC9IVRw out'), 'https://youtu.be/jNQXAC9IVRw');
  assert.equal(findYouTubeUrl('https://www.youtube.com/watch?v=dQw4w9WgXcQ'), 'https://www.youtube.com/watch?v=dQw4w9WgXcQ');
  assert.equal(findYouTubeUrl('no link here'), null);
  assert.equal(findYouTubeUrl('<https://youtu.be/jNQXAC9IVRw>'), 'https://youtu.be/jNQXAC9IVRw');
});
