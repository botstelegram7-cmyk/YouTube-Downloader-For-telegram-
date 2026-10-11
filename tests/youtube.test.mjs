import test from 'node:test';
import assert from 'node:assert/strict';
import { normalizeYouTubeUrl, parseCommand, QUALITIES } from '../tgcloud/lib/youtube.js';

test('normalizes a YouTube URL without a scheme', () => {
  assert.equal(normalizeYouTubeUrl('youtu.be/abc123'), 'https://youtu.be/abc123');
});

test('accepts common YouTube video and Shorts URLs', () => {
  for (const input of [
    'https://www.youtube.com/watch?v=abc123',
    'youtube.com/shorts/abc123',
    'https://music.youtube.com/watch?v=abc123'
  ]) {
    assert.ok(normalizeYouTubeUrl(input), `expected URL to be accepted: ${input}`);
  }
});

test('rejects unrelated hosts, credentials, and unsupported protocols', () => {
  for (const input of [
    'https://youtube.com.evil.example/watch?v=abc',
    'https://youtube.com@evil.example/watch?v=abc',
    'ftp://youtube.com/watch?v=abc',
    'https://youtube.com',
    'not a url'
  ]) {
    assert.equal(normalizeYouTubeUrl(input), null, `expected URL to be rejected: ${input}`);
  }
});

test('parses commands case-insensitively and ignores bot usernames', () => {
  assert.deepEqual(parseCommand('/START@PyTuber_bot 720p'), {
    command: '/start',
    args: ['720p']
  });
});

test('handles empty command input', () => {
  assert.deepEqual(parseCommand('   '), { command: '', args: [] });
});

test('contains the supported quality choices', () => {
  assert.deepEqual([...QUALITIES], ['360p', '480p', '720p', '1080p', '1440p', '2160p']);
});
