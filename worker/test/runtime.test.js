import test from 'node:test';
import assert from 'node:assert/strict';
import zlib from 'node:zlib';

import {
  TypeTubeUnsupportedRuntimeError,
  installZlibCompat,
  isTypeTubeRuntimeSupported,
  typeTubeRuntimeReport
} from '../src/typetube.js';
import { supportsZstd } from '../src/config.js';
import { isM4aCompatible, parseDurationFromFfmpeg } from '../src/ffmpeg.js';
import { TelegramApiError } from '../src/telegram.js';

test('installZlibCompat is a no-op on a zstd-capable runtime', () => {
  const installed = installZlibCompat();
  if (supportsZstd()) {
    assert.equal(installed, false, 'nothing to shim on Node >= 22.15');
  } else {
    assert.equal(installed, true, 'older runtimes get the import safety net');
  }
});

test('the compat shim never removes real zstd functions', () => {
  installZlibCompat();
  if (supportsZstd()) {
    assert.equal(typeof zlib.zstdCompress, 'function');
    assert.equal(typeof zlib.zstdDecompressSync, 'function');
  } else {
    // The shim only exists to turn a confusing import crash into a clear error.
    assert.equal(typeof zlib.zstdCompress, 'function');
    assert.equal(typeof zlib.brotliCompressSync, 'function');
  }
});

test('a runtime without zstd is reported as unsupported, with an actionable message', async () => {
  const report = typeTubeRuntimeReport();
  assert.equal(typeof report.supported, 'boolean');
  assert.equal(report.supported, isTypeTubeRuntimeSupported());

  if (!report.supported) {
    const error = new TypeTubeUnsupportedRuntimeError();
    assert.equal(error.code, 'UNSUPPORTED_RUNTIME');
    assert.match(error.message, /Node\.js >= 22\.15/);
    assert.match(error.message, /YTDLP_FALLBACK/);
  }
});

test('Node version gate matches the documented release boundaries', () => {
  assert.equal(supportsZstd('v18.20.0'), false);
  assert.equal(supportsZstd('v20.20.2'), false);
  assert.equal(supportsZstd('v22.15.0'), true);
  assert.equal(supportsZstd('v24.9.0'), true);
});

test('M4A detection decides whether Telegram gets music or a document', () => {
  assert.equal(isM4aCompatible('audio/mp4; codecs="mp4a.40.2"'), true);
  assert.equal(isM4aCompatible('audio/m4a'), true);
  assert.equal(isM4aCompatible('audio/webm; codecs="opus"'), false);
  assert.equal(isM4aCompatible(''), false);
});

test('ffmpeg duration parsing works without ffprobe', () => {
  const line = '  Duration: 00:03:33.06, start: 0.000000, bitrate: 128 kb/s';
  assert.equal(parseDurationFromFfmpeg(line), 213.06);
  assert.equal(parseDurationFromFfmpeg('no duration here'), null);
});

test('TelegramApiError classifies the failures we act on', () => {
  const tooLarge = new TelegramApiError('sendVideo', 'Bad Request: file is too big', { code: 400 });
  assert.equal(tooLarge.isTooLarge, true);
  assert.equal(tooLarge.isRateLimited, false);

  const rateLimited = new TelegramApiError('sendVideo', 'Too Many Requests', {
    code: 429,
    parameters: { retry_after: 12 }
  });
  assert.equal(rateLimited.isRateLimited, true);
  assert.equal(rateLimited.retryAfterSeconds, 12);

  const notModified = new TelegramApiError('editMessageText', 'Bad Request: message is not modified', { code: 400 });
  assert.equal(notModified.isNotModified, true);
});
