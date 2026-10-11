/**
 * Environment loading, validation and documentation for the download worker.
 *
 * Every setting is read once at boot, validated as a group, and frozen. Invalid
 * configuration produces a single readable report listing *all* problems instead
 * of the first one, because on a hosting dashboard you usually fix them in one go.
 */

export const NODE_MIN_MAJOR = 22;
export const NODE_MIN_MINOR = 15;

/** Bot tokens look like `123456789:AA...`; we only sanity-check the shape. */
const BOT_TOKEN_PATTERN = /^\d{6,}:[A-Za-z0-9_-]{30,}$/;

const LOG_LEVELS = ['debug', 'info', 'warn', 'error'];

const TRUTHY = new Set(['1', 'true', 'yes', 'on']);
const FALSY = new Set(['0', 'false', 'no', 'off', '']);

const SPEC = {
  BOT_TOKEN: { type: 'string', secret: true, required: true, describe: 'BotFather bot token used for uploads and status edits' },
  WORKER_SECRET: { type: 'string', secret: true, required: true, minLength: 16, describe: 'Shared secret that authorises the Serverless handler' },
  PORT: { type: 'int', default: 8080, min: 1, max: 65535, describe: 'HTTP port (Render sets this automatically)' },
  MAX_CONCURRENT_DOWNLOADS: { type: 'int', default: 1, min: 1, max: 8, describe: 'Jobs processed at the same time (keep 1 on a free instance)' },
  JOB_TIMEOUT_SECONDS: { type: 'int', default: 900, min: 60, max: 3600, describe: 'Hard limit for one job, download + mux + upload' },
  RESOLVE_TIMEOUT_SECONDS: { type: 'int', default: 60, min: 10, max: 300, describe: 'Timeout for resolving a video to streams' },
  DOWNLOAD_WORKERS: { type: 'int', default: 4, min: 1, max: 32, describe: 'Parallel range workers used by TypeTube (4 is safe on 512 MB)' },
  CHUNK_SIZE_MB: { type: 'int', default: 8, min: 1, max: 64, describe: 'Range chunk size for TypeTube downloads' },
  MAX_UPLOAD_MB: { type: 'int', default: 0, min: 0, max: 2000, describe: 'Largest file we send to Telegram; 0 removes the worker-side limit (Telegram still applies its own)' },
  YTDLP_FALLBACK: { type: 'bool', default: true, describe: 'Use yt-dlp when TypeTube cannot deliver the video' },
  YTDLP_TIMEOUT_SECONDS: { type: 'int', default: 600, min: 60, max: 3600, describe: 'Timeout for the yt-dlp fallback' },
  AUTO_DOWNGRADE: { type: 'bool', default: true, describe: 'Retry at a lower quality when the result exceeds MAX_UPLOAD_MB' },
  MAX_DOWNGRADE_ATTEMPTS: { type: 'int', default: 2, min: 0, max: 3, describe: 'How many times a too-large result may be re-downloaded smaller' },
  STATUS_UPDATE_SECONDS: { type: 'int', default: 6, min: 2, max: 60, describe: 'Minimum gap between progress edits of the Telegram status message' },
  TYPETUBE_API_KEY: { type: 'string', default: '', secret: true, describe: 'Optional private TypeTube key (unlocks 256 kbps audio + higher quotas)' },
  TYPETUBE_HOST: { type: 'string', default: '', describe: 'Optional TypeTube cluster host (defaults to the public cluster)' },
  TYPETUBE_PORT: { type: 'int', default: 0, min: 0, max: 65535, describe: 'Optional TypeTube cluster port' },
  TELEGRAM_API_BASE: { type: 'string', default: 'https://api.telegram.org', describe: 'Bot API base URL (point this at a self-hosted bot API server to raise the upload limit)' },
  STRICT_STARTUP: { type: 'bool', default: false, describe: 'Refuse to boot when TypeTube cannot run on this Node version' },
  LOG_LEVEL: { type: 'string', default: 'info', enum: LOG_LEVELS, describe: 'debug | info | warn | error' },
  LOG_PRETTY: { type: 'bool', default: false, describe: 'Human-readable logs instead of single-line JSON' }
};

export class ConfigError extends Error {
  constructor(problems) {
    super(
      'Invalid worker configuration:\n' +
        problems.map((line) => `  - ${line}`).join('\n') +
        '\n\nSee worker/.env.example and docs/DEPLOYMENT.md for every variable.'
    );
    this.name = 'ConfigError';
    this.problems = problems;
  }
}

function coerce(name, spec, raw, problems) {
  if (raw === undefined || raw === null || raw === '') {
    if (spec.required) {
      problems.push(`${name} is required (${spec.describe})`);
      return spec.default ?? '';
    }
    return spec.default;
  }

  const value = String(raw).trim();

  if (spec.type === 'int') {
    const parsed = Number(value);
    if (!Number.isInteger(parsed)) {
      problems.push(`${name} must be an integer, received "${truncate(value)}"`);
      return spec.default;
    }
    if (spec.min !== undefined && parsed < spec.min) {
      problems.push(`${name} must be >= ${spec.min}, received ${parsed}`);
      return spec.default;
    }
    if (spec.max !== undefined && parsed > spec.max) {
      problems.push(`${name} must be <= ${spec.max}, received ${parsed}`);
      return spec.default;
    }
    return parsed;
  }

  if (spec.type === 'bool') {
    const lowered = value.toLowerCase();
    if (TRUTHY.has(lowered)) return true;
    if (FALSY.has(lowered)) return false;
    problems.push(`${name} must be a boolean (true/false), received "${truncate(value)}"`);
    return spec.default;
  }

  if (spec.enum && !spec.enum.includes(value)) {
    problems.push(`${name} must be one of ${spec.enum.join(', ')}, received "${truncate(value)}"`);
    return spec.default;
  }

  if (spec.minLength && value.length < spec.minLength) {
    problems.push(`${name} must be at least ${spec.minLength} characters (received ${value.length})`);
  }

  return value;
}

function truncate(value, max = 24) {
  const text = String(value);
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

/**
 * @param {Record<string, string | undefined>} env
 * @returns {{ config: object, problems: string[], warnings: string[] }}
 */
export function loadConfig(env = process.env) {
  const problems = [];
  const warnings = [];
  const config = {};

  for (const [name, spec] of Object.entries(SPEC)) {
    config[name] = coerce(name, spec, env[name], problems);
  }

  if (config.BOT_TOKEN && !BOT_TOKEN_PATTERN.test(config.BOT_TOKEN)) {
    if (config.BOT_TOKEN.startsWith('app') || config.BOT_TOKEN.includes(':')) {
      warnings.push(
        'BOT_TOKEN does not look like a BotFather bot token. If you pasted the Serverless CLI access token ' +
          '(app<id>:<secret>) the worker cannot upload files — get the bot token from @BotFather → API Token.'
      );
    } else {
      problems.push('BOT_TOKEN does not look like a valid bot token (expected 123456789:AA...)');
    }
  }

  if (config.WORKER_SECRET && config.WORKER_SECRET.length < 32) {
    warnings.push('WORKER_SECRET is shorter than 32 characters; generate one with `openssl rand -hex 32`.');
  }

  if (config.MAX_CONCURRENT_DOWNLOADS > 1) {
    warnings.push(
      `MAX_CONCURRENT_DOWNLOADS=${config.MAX_CONCURRENT_DOWNLOADS} multiplies peak memory; ` +
        'use 1 on 512 MB instances.'
    );
  }

  if (config.DOWNLOAD_WORKERS > 8) {
    warnings.push(`DOWNLOAD_WORKERS=${config.DOWNLOAD_WORKERS} can exhaust memory on small instances (4 is recommended).`);
  }

  const port = Number(env.PORT);
  if (Number.isInteger(port) && port > 0 && port !== config.PORT) {
    // PORT is authoritative for the platform; keep the operator informed.
    warnings.push(`PORT=${env.PORT} came from the environment and overrides the default.`);
  }

  // 0 means "no limit of our own": the Telegram API is the only thing that can refuse
  // the file. `Infinity` keeps every size comparison downstream unchanged.
  config.MAX_UPLOAD_BYTES = config.MAX_UPLOAD_MB > 0 ? config.MAX_UPLOAD_MB * 1_000_000 : Infinity;
  config.CHUNK_SIZE_BYTES = config.CHUNK_SIZE_MB * 1024 * 1024;
  config.JOB_TIMEOUT_MS = config.JOB_TIMEOUT_SECONDS * 1000;
  config.RESOLVE_TIMEOUT_MS = config.RESOLVE_TIMEOUT_SECONDS * 1000;
  config.YTDLP_TIMEOUT_MS = config.YTDLP_TIMEOUT_SECONDS * 1000;
  config.TELEGRAM_API_BASE = String(config.TELEGRAM_API_BASE || 'https://api.telegram.org').replace(/\/+$/, '');
  if (!/^https?:\/\//.test(config.TELEGRAM_API_BASE)) {
    problems.push('TELEGRAM_API_BASE must be an http(s) URL');
  }
  config.TELEGRAM_API_BASE_IS_CUSTOM = config.TELEGRAM_API_BASE !== 'https://api.telegram.org';

  if (config.MAX_UPLOAD_MB === 0) {
    if (config.TELEGRAM_API_BASE_IS_CUSTOM) {
      warnings.push(
        'MAX_UPLOAD_MB=0: uploads are not capped by this worker. With a self-hosted bot API server the ' +
          'hard ceiling is 2 GB per file.'
      );
    } else {
      warnings.push(
        'MAX_UPLOAD_MB=0 removes the worker-side limit, but the hosted Bot API still refuses files over ' +
          '50 MB. Set TELEGRAM_API_BASE to a self-hosted bot API server to send more, or set MAX_UPLOAD_MB ' +
          'to a number so the worker can downgrade quality before uploading.'
      );
    }
  }
  config.VERSION = '2.0.1';

  return { config, problems, warnings };
}

/** Loads the environment and throws a single, readable error when it is unusable. */
export function requireConfig(env = process.env) {
  const { config, problems, warnings } = loadConfig(env);
  if (problems.length > 0) throw new ConfigError(problems);
  return { config, warnings };
}

/** `true` when the running Node can decompress the zstd frames TypeTube sends. */
export function supportsZstd(nodeVersion = process.version) {
  const [major, minor] = String(nodeVersion).replace(/^v/, '').split('.').map(Number);
  if (!Number.isFinite(major) || !Number.isFinite(minor)) return false;
  // zlib zstd landed in v23.8.0 and was backported to v22.15.0, so runs of
  // v23 need their own check; everything from v24 on has it unconditionally.
  if (major >= 24) return true;
  if (major === 23) return minor >= 8;
  if (major === 22) return minor >= 15;
  return false;
}

/** Human-readable summary of the non-secret configuration, for logs and /health. */
export function describeConfig(config) {
  return {
    version: config.VERSION,
    port: config.PORT,
    maxConcurrentDownloads: config.MAX_CONCURRENT_DOWNLOADS,
    jobTimeoutSeconds: config.JOB_TIMEOUT_SECONDS,
    downloadWorkers: config.DOWNLOAD_WORKERS,
    chunkSizeMb: config.CHUNK_SIZE_MB,
    maxUploadMb: config.MAX_UPLOAD_MB > 0 ? config.MAX_UPLOAD_MB : 'unlimited',
    ytDlpFallback: config.YTDLP_FALLBACK,
    autoDowngrade: config.AUTO_DOWNGRADE,
    typeTubeKey: config.TYPETUBE_API_KEY ? 'private' : 'public',
    telegramApi: config.TELEGRAM_API_BASE_IS_CUSTOM ? 'self-hosted' : 'hosted',
    logLevel: config.LOG_LEVEL
  };
}
