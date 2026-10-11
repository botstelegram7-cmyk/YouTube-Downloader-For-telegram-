import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

import {
  QUALITIES,
  heightOf,
  isMode,
  isQuality,
  nextLowerQuality,
  normalizeYouTubeUrl,
  safeFileName,
  thumbnailFor,
  youtubeVideoId
} from '../src/validate.js';

const fixtures = JSON.parse(
  readFileSync(path.join(path.dirname(fileURLToPath(import.meta.url)), 'fixtures/url_vectors.json'), 'utf8')
);

test('accepts every URL shape YouTube actually hands out', () => {
  for (const input of fixtures.accepted) {
    const normalized = normalizeYouTubeUrl(input);
    assert.ok(normalized, `expected accepted: ${input}`);
    assert.match(normalized, /^https:\/\//, `expected https: ${input}`);
  }
});

test('rejects lookalike hosts, credentials, schemes and empty paths', () => {
  for (const input of fixtures.rejected) {
    assert.equal(normalizeYouTubeUrl(input), null, `expected rejected: ${input}`);
  }
});

test('rejects non-string input instead of throwing', () => {
  for (const value of [null, undefined, 42, {}, [], Symbol('x')]) {
    assert.equal(normalizeYouTubeUrl(value), null);
  }
});

test('refuses absurdly long input before parsing it', () => {
  assert.equal(normalizeYouTubeUrl(`https://youtu.be/${'a'.repeat(3000)}`), null);
});

test('extracts video ids from every supported path', () => {
  for (const { url, id } of fixtures.videoIds) {
    assert.equal(youtubeVideoId(url), id, `for ${url}`);
  }
});

test('quality helpers agree with each other', () => {
  assert.deepEqual([...QUALITIES], ['360p', '480p', '720p', '1080p', '1440p', '2160p']);
  assert.equal(heightOf('1080p'), 1080);
  assert.equal(heightOf('1080'), 0);
  assert.equal(heightOf(undefined), 0);
  assert.equal(nextLowerQuality('1080p'), '720p');
  assert.equal(nextLowerQuality('1440p'), '1080p');
  assert.equal(nextLowerQuality('360p'), null);
  assert.equal(isQuality('720p'), true);
  assert.equal(isQuality('720'), false);
  assert.equal(isMode('audio'), true);
  assert.equal(isMode('gif'), false);
});

test('thumbnails are only produced for real video ids', () => {
  assert.equal(thumbnailFor('jNQXAC9IVRw'), 'https://i.ytimg.com/vi/jNQXAC9IVRw/mqdefault.jpg');
  assert.equal(thumbnailFor('nope'), null);
});

test('file names survive hostile titles', () => {
  assert.equal(safeFileName('AC/DC: Back in Black?'), 'AC DC Back in Black');
  assert.equal(safeFileName(''), 'video');
  assert.equal(safeFileName('   ...  '), 'video');
  assert.equal(safeFileName('x'.repeat(200)).length, 90);
  assert.equal(safeFileName('null\u0000byte'), 'nullbyte');
});
