import test from 'node:test';
import assert from 'node:assert/strict';

import { ConfigError, describeConfig, loadConfig, requireConfig, supportsZstd } from '../src/config.js';

const VALID = {
  BOT_TOKEN: '123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
  WORKER_SECRET: 'a'.repeat(48)
};

test('a minimal environment produces a usable configuration', () => {
  const { config, problems } = loadConfig({ ...VALID });
  assert.deepEqual(problems, []);
  assert.equal(config.PORT, 8080);
  assert.equal(config.MAX_CONCURRENT_DOWNLOADS, 1);
  assert.equal(config.MAX_UPLOAD_BYTES, 50_000_000);
  assert.equal(config.CHUNK_SIZE_BYTES, 8 * 1024 * 1024);
  assert.equal(config.JOB_TIMEOUT_MS, 900_000);
});

test('the platform PORT wins over the default', () => {
  const { config } = loadConfig({ ...VALID, PORT: '10000' });
  assert.equal(config.PORT, 10000);
});

test('reports every problem at once instead of stopping at the first', () => {
  const { problems } = loadConfig({ PORT: 'abc', MAX_CONCURRENT_DOWNLOADS: '99', LOG_LEVEL: 'loud' });
  assert.equal(problems.length, 5, problems.join(' | '));
  assert.ok(problems.some((line) => line.startsWith('BOT_TOKEN is required')));
  assert.ok(problems.some((line) => line.startsWith('WORKER_SECRET is required')));
  assert.ok(problems.some((line) => line.includes('PORT must be an integer')));
  assert.ok(problems.some((line) => line.includes('MAX_CONCURRENT_DOWNLOADS must be <= 8')));
  assert.ok(problems.some((line) => line.includes('LOG_LEVEL must be one of')));
});

test('requireConfig throws one readable error listing everything', () => {
  assert.throws(
    () => requireConfig({}),
    (error) => {
      assert.ok(error instanceof ConfigError);
      assert.match(error.message, /Invalid worker configuration/);
      assert.match(error.message, /BOT_TOKEN is required/);
      assert.match(error.message, /WORKER_SECRET is required/);
      return true;
    }
  );
});

test('warns when the Serverless CLI token is pasted as BOT_TOKEN', () => {
  const { warnings } = loadConfig({ ...VALID, BOT_TOKEN: 'app12345678:abcdefghijklmnopqrstuvwxyz' });
  assert.ok(warnings.some((line) => line.includes('Serverless CLI access token')));
});

test('warns about settings that are unsafe on a small instance', () => {
  const { warnings } = loadConfig({ ...VALID, MAX_CONCURRENT_DOWNLOADS: '4', DOWNLOAD_WORKERS: '16' });
  assert.ok(warnings.some((line) => line.includes('MAX_CONCURRENT_DOWNLOADS=4')));
  assert.ok(warnings.some((line) => line.includes('DOWNLOAD_WORKERS=16')));
});

test('booleans accept the spellings people actually type', () => {
  const truthy = loadConfig({ ...VALID, YTDLP_FALLBACK: 'yes', AUTO_DOWNGRADE: 'TRUE' });
  assert.equal(truthy.config.YTDLP_FALLBACK, true);
  assert.equal(truthy.config.AUTO_DOWNGRADE, true);

  const falsy = loadConfig({ ...VALID, YTDLP_FALLBACK: 'off', AUTO_DOWNGRADE: '0' });
  assert.equal(falsy.config.YTDLP_FALLBACK, false);
  assert.equal(falsy.config.AUTO_DOWNGRADE, false);
});

test('supportsZstd follows the Node release boundaries', () => {
  assert.equal(supportsZstd('v20.20.2'), false);
  assert.equal(supportsZstd('v22.14.0'), false);
  assert.equal(supportsZstd('v22.15.0'), true);
  assert.equal(supportsZstd('v23.7.0'), false);
  assert.equal(supportsZstd('v23.8.0'), true);
  assert.equal(supportsZstd('v24.9.0'), true);
  assert.equal(supportsZstd('garbage'), false);
});

test('describeConfig never leaks a secret', () => {
  const { config } = loadConfig({ ...VALID, TYPETUBE_API_KEY: 'private-key-value' });
  const description = JSON.stringify(describeConfig(config));
  assert.ok(!description.includes(VALID.WORKER_SECRET));
  assert.ok(!description.includes(VALID.BOT_TOKEN));
  assert.ok(!description.includes('private-key-value'));
  assert.ok(description.includes('"typeTubeKey":"private"'));
});
