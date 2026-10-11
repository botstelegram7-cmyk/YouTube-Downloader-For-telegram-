import test from 'node:test';
import assert from 'node:assert/strict';

import { audioFormatSelector, describeFailure, isPermanentFailure, parseProgressLine, videoFormatSelector } from '../src/ytdlp.js';
import { explainError, retryKeyboard, smallerQualities, buildCaption } from '../src/job.js';
import { CancelledError, TooLargeError, formatMb, videoFormatLabel } from '../src/downloader.js';
import { TelegramApiError } from '../src/telegram.js';
import { StatusUpdater } from '../src/job.js';

test('the yt-dlp format ladder prefers mp4/m4a so Telegram accepts the merge', () => {
  const selector = videoFormatSelector('1080p');
  assert.match(selector, /bv\*\[height<=1080\]\[ext=mp4\]\+ba\[ext=m4a\]/);
  assert.match(selector, /b\[height<=1080\]/);
  assert.equal(audioFormatSelector(), 'ba[ext=m4a]/ba/b');
});

test('yt-dlp progress lines are parsed into percentages', () => {
  const progress = parseProgressLine('[download]  45.2% of  12.34MiB at  1.05MiB/s ETA 00:11');
  assert.equal(progress.percent, 45.2);
  assert.equal(progress.speed, '1.05MiB/s');
  assert.equal(progress.eta, '00:11');

  assert.equal(parseProgressLine('[youtube] Extracting URL'), null);
  assert.equal(parseProgressLine(''), null);
});

test('yt-dlp failures are translated into human sentences', () => {
  assert.match(describeFailure('ERROR: Private video. Sign in if you have been granted access'), /private/i);
  assert.match(describeFailure('Join this channel to get access to members-only content'), /members only/i);
  assert.match(describeFailure('Sign in to confirm you are not a bot'), /bot check/i);
  assert.match(describeFailure('ERROR: This video is not available in your country'), /unavailable or blocked/i);
  assert.equal(describeFailure('some other error'), null);

  assert.equal(isPermanentFailure('ERROR: Private video'), true);
  assert.equal(isPermanentFailure('ERROR: HTTP Error 500'), false);
});

test('TooLargeError produces a downgrade suggestion built from the ladder', () => {
  const { text, retryQualities } = explainError(new TooLargeError(78_000_000, 50_000_000, { quality: '1080p' }));
  assert.match(text, /78\.0 MB/);
  assert.match(text, /50 MB/);
  assert.deepEqual(retryQualities, ['720p', '480p', '360p']);
});

test('the retry keyboard only offers real qualities and caps at three buttons', () => {
  const keyboard = retryKeyboard('job-123', ['720p', '480p', '360p', 'nonsense']);
  assert.equal(keyboard.inline_keyboard.length, 1);
  assert.deepEqual(
    keyboard.inline_keyboard[0].map((button) => button.callback_data),
    ['dl:720p:job-123', 'dl:480p:job-123', 'dl:360p:job-123']
  );
  assert.equal(retryKeyboard('job-123', []), undefined);
});

test('smallerQualities walks down the ladder without falling off it', () => {
  assert.deepEqual(smallerQualities('2160p', 3), ['1440p', '1080p', '720p']);
  assert.deepEqual(smallerQualities('480p', 5), ['360p']);
  assert.deepEqual(smallerQualities('bogus', 2), []);
});

test('cancellation and unknown errors never leak a stack trace to the user', () => {
  const cancelled = explainError(new CancelledError());
  assert.match(cancelled.text, /cancelled/i);

  const unknown = explainError(new Error('TypeTube exploded at buffer offset 42'));
  assert.match(unknown.text, /Download failed/);
  assert.ok(unknown.text.length < 400);
});

test('a Telegram "file is too big" rejection becomes the downgrade message', () => {
  const error = new TelegramApiError('sendVideo', 'Bad Request: file is too big', { code: 400 });
  const { text, retryQualities } = explainError(error, { maxUploadMb: 50, quality: '1080p' });
  assert.match(text, /upload limit/i);
  assert.deepEqual(retryQualities, ['720p', '480p', '360p']);
});

test('with the worker limit removed, the message explains Telegram-s own ceilings', () => {
  const error = new TelegramApiError('sendVideo', 'Bad Request: file is too big', { code: 400 });
  const { text, retryQualities } = explainError(error, { maxUploadMb: 0, quality: '1080p' });
  assert.match(text, /50 MB on the hosted Bot API/);
  assert.match(text, /2 GB on a self-hosted server/);
  assert.ok(!text.includes('above Telegram\'s 0 MB'), text);
  assert.deepEqual(retryQualities, ['720p', '480p', '360p']);
});

test('the 50 MB downgrade message mentions the smaller quality command', () => {
  const { text } = explainError(new TooLargeError(60_000_000, 50_000_000, { quality: '720p' }));
  assert.match(text, /\/download 480p/);
});

test('regional 403s explain the Singapore cluster relationship', () => {
  const { text } = explainError(new Error('Stream download forbidden (HTTP 403): track restricted'));
  assert.match(text, /region/i);
  assert.match(text, /Singapore/);
});

test('captions carry the essentials and stay inside Telegram limits', () => {
  const caption = buildCaption({
    title: 'Never Gonna Give You Up',
    mode: 'video',
    formatLabel: '1080p (H.264)',
    sizeBytes: 12_345_678,
    source: 'typetube',
    hasAudio: true
  });
  assert.match(caption, /Never Gonna Give You Up/);
  assert.match(caption, /1080p \(H\.264\)/);
  assert.match(caption, /12\.3 MB/);
  assert.match(caption, /via TypeTube/);
  assert.ok(caption.length <= 900);

  assert.match(buildCaption({ title: 'Song', mode: 'audio' }), /^🎵 Song/);
  assert.match(buildCaption({ title: 'Clip', mode: 'video', hasAudio: false }), /no audio track/);
});

test('formatMb formats megabytes the way captions expect', () => {
  assert.equal(formatMb(0), '0.0 MB');
  assert.equal(formatMb(49_999_999), '50.0 MB');
});

test('videoFormatLabel names the codec when it knows it', () => {
  assert.equal(videoFormatLabel({ mimeType: 'video/mp4; codecs="avc1.640028"', resolution: '1920x1080' }, '1080p'), '1920x1080 (H.264)');
  assert.equal(videoFormatLabel({ mimeType: 'video/webm; codecs="vp9"', quality: '1080p' }, '1080p'), '1080p (VP9)');
  assert.equal(videoFormatLabel(undefined, '720p'), '720p');
});

test('StatusUpdater throttles edits but always sends the newest text', async () => {
  const edits = [];
  const telegram = {
    editStatus: async (chatId, messageId, text) => {
      edits.push(text);
      return true;
    },
    call: async (method, params) => {
      edits.push(params.text);
      return { ok: true };
    }
  };
  const updater = new StatusUpdater({ telegram, chatId: 1, messageId: 2, minIntervalMs: 10_000 });

  assert.equal(await updater.update('first'), true);
  assert.equal(await updater.update('second'), false, 'inside the throttle window');
  assert.equal(await updater.update('first'), false, 'duplicate text');
  assert.equal(await updater.update('forced', { force: true }), true);
  await updater.final('done');

  assert.deepEqual(edits, ['first', 'forced', 'done']);
});

test('StatusUpdater swallows "message is not modified" and 400s', async () => {
  const telegram = {
    editStatus: async () => {
      throw new TelegramApiError('editMessageText', 'Bad Request: message is not modified', { code: 400 });
    },
    call: async () => {
      throw new TelegramApiError('editMessageText', 'Bad Request: message to edit not found', { code: 400 });
    }
  };
  const updater = new StatusUpdater({ telegram, chatId: 1, messageId: 2, minIntervalMs: 0 });
  assert.equal(await updater.update('hello'), false);
  assert.equal(await updater.final('bye'), false);
});
