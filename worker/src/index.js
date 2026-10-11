#!/usr/bin/env node
/**
 * Worker entry point.
 *
 * Boot order matters on a hosting platform:
 *   1. read and validate the environment (fail fast, with every problem listed);
 *   2. build Telegram + downloader + queue;
 *   3. start listening on 0.0.0.0:$PORT so the platform health check passes;
 *   4. only then report any capability problem (e.g. Node too old for TypeTube).
 *
 * A capability problem must not stop the process: if it did, Render would show a
 * crash loop instead of a readable /health response. STRICT_STARTUP=true flips that
 * for operators who prefer a loud failure.
 */

import http from 'node:http';
import { ConfigError, describeConfig, loadConfig, supportsZstd } from './config.js';
import { Logger } from './logger.js';
import { TelegramClient } from './telegram.js';
import { Downloader } from './downloader.js';
import { processJob } from './job.js';
import { createQueue, createRequestHandler } from './server.js';
import { sweepStaleJobDirs } from './workspace.js';
import { isTypeTubeRuntimeSupported, typeTubeRuntimeReport } from './typetube.js';
import { ffmpegPath } from './ffmpeg.js';

async function main() {
  const { config, problems, warnings } = loadConfig(process.env);
  const log = new Logger({ level: config.LOG_LEVEL, pretty: config.LOG_PRETTY, base: { service: 'worker' } });

  for (const warning of warnings) log.warn(warning);

  if (problems.length > 0) {
    for (const problem of problems) log.error(problem);
    throw new ConfigError(problems);
  }

  const typeTubeReady = isTypeTubeRuntimeSupported();
  const ffmpeg = await ffmpegPath();

  log.info('Worker starting', {
    ...describeConfig(config),
    node: process.version,
    zstd: supportsZstd(),
    typeTube: typeTubeReady ? 'enabled' : 'disabled',
    ffmpeg: ffmpeg ?? 'missing'
  });

  if (!typeTubeReady) {
    const report = typeTubeRuntimeReport();
    log.warn('TypeTube is unavailable on this runtime', { note: report.note });
    if (config.STRICT_STARTUP) {
      throw new Error(report.note);
    }
    if (!config.YTDLP_FALLBACK) {
      log.error('Both TypeTube and the yt-dlp fallback are unavailable — every job will fail.');
    }
  }

  if (!ffmpeg) {
    log.warn('FFmpeg was not found: video+audio merging and audio conversion are degraded.');
  }

  await sweepStaleJobDirs(log);

  const telegram = new TelegramClient({ token: config.BOT_TOKEN, log, apiBase: config.TELEGRAM_API_BASE });

  try {
    const me = await telegram.getMe();
    log.info('Telegram bot verified', { username: me?.username, id: me?.id });
  } catch (error) {
    // Not fatal: the token may be valid but the network may still be starting up.
    log.error('Could not verify the bot token with getMe', { message: error.message });
  }

  const downloader = new Downloader({ config, log });

  const queue = createQueue({
    config,
    log,
    processJob: (job, deps) => processJob(job, { ...deps, telegram, downloader })
  });

  const handler = createRequestHandler({ config, log, queue });

  const server = http.createServer((request, response) => {
    handler(request, response).catch((error) => {
      log.error('Unhandled handler error', { message: error?.message, stack: error?.stack });
      if (!response.headersSent) {
        response.writeHead(500, { 'content-type': 'application/json' });
      }
      response.end(JSON.stringify({ ok: false, error: 'internal error' }));
    });
  });

  server.keepAliveTimeout = 65_000;
  server.headersTimeout = 70_000;
  server.requestTimeout = 60_000;

  const port = config.PORT;

  await new Promise((resolve, reject) => {
    server.once('error', reject);
    server.listen(port, '0.0.0.0', () => {
      server.removeListener('error', reject);
      resolve();
    });
  });

  log.info('Worker listening', { port, url: `http://0.0.0.0:${port}` });

  let shuttingDown = false;
  const shutdown = async (signal) => {
    if (shuttingDown) return;
    shuttingDown = true;
    log.info('Shutdown requested', { signal, running: queue.running, pending: queue.pending });

    const signalled = queue.cancelAll(`worker shutting down (${signal})`);
    if (signalled > 0) log.info('Signalled jobs to stop', { signalled });

    const graceMs = Math.min(config.JOB_TIMEOUT_MS, 30_000);
    const finished = await queue.drain(graceMs);
    if (!finished) log.warn('Some jobs were still running when the grace period ended');

    server.close(() => {
      log.info('Worker stopped');
      process.exit(0);
    });

    setTimeout(() => process.exit(0), 5_000).unref();
  };

  process.on('SIGTERM', () => void shutdown('SIGTERM'));
  process.on('SIGINT', () => void shutdown('SIGINT'));
  process.on('unhandledRejection', (reason) => {
    log.error('Unhandled promise rejection', { message: reason?.message ?? String(reason), stack: reason?.stack });
  });
  process.on('uncaughtException', (error) => {
    log.error('Uncaught exception', { message: error.message, stack: error.stack });
  });
}

main().catch((error) => {
  // The logger may not exist yet when the configuration itself is broken.
  const isConfig = error instanceof ConfigError;
  const prefix = isConfig ? '' : 'Worker failed to start: ';
  process.stderr.write(`${prefix}${error?.message ?? error}\n`);
  process.exit(1);
});
