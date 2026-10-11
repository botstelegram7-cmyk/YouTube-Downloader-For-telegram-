/**
 * TypeTube runtime adapter.
 *
 * Why this file exists (and why it is not a plain `import`):
 *
 * `typetube@1.1.4` ships `dist/tap/compression.js`, which calls
 * `promisify(zlib.zstdCompress)` at module scope. `zlib.zstdCompress` only exists
 * on Node >= 22.15 / >= 23.8, so on older runtimes `promisify(undefined)` throws
 * `ERR_INVALID_ARG_TYPE` *while importing the package* — the crash happens before
 * any of our code runs and the message points at zlib, not at the real cause.
 *
 * Two things follow:
 *  1. The worker must lazily `import()` TypeTube (after this module has installed a
 *     safety net and verified the runtime) so the failure is a clear, actionable
 *     error instead of a stack trace from inside node:util.
 *  2. TypeTube's cluster answers with zstd-compressed TAP frames. Without a real
 *     zstd decompressor those frames cannot be read at all, so a brotli shim is
 *     *not* a fix — it only keeps the import from exploding. We therefore refuse
 *     to run rather than fail mysteriously later (see docs/TYPETUBE_COMPAT.md).
 */

import zlib from 'node:zlib';
import { NODE_MIN_MAJOR, NODE_MIN_MINOR, supportsZstd } from './config.js';

export class TypeTubeUnsupportedRuntimeError extends Error {
  constructor(nodeVersion = process.version) {
    super(
      `TypeTube requires Node.js >= ${NODE_MIN_MAJOR}.${NODE_MIN_MINOR} (running ${nodeVersion}). ` +
        `Node ${NODE_MIN_MAJOR}.${NODE_MIN_MINOR} is the first release with zlib zstd support, which the ` +
        'TypeTube cluster needs to decompress its TAP responses. Upgrade the runtime or let the worker ' +
        'fall back to yt-dlp (set YTDLP_FALLBACK=true, STRICT_STARTUP=false).'
    );
    this.name = 'TypeTubeUnsupportedRuntimeError';
    this.code = 'UNSUPPORTED_RUNTIME';
    this.nodeVersion = nodeVersion;
  }
}

let shimInstalled = false;
let modulePromise = null;
let client = null;
let lastClientConfig = null;

/**
 * Keeps the package import from throwing on older Node by giving zlib a brotli-backed
 * stand-in. This does not make TypeTube functional there — see the file header — but a
 * later, honest error beats `ERR_INVALID_ARG_TYPE` at import time.
 * @returns {boolean} true when a shim had to be installed
 */
export function installZlibCompat() {
  if (supportsZstd()) return false;
  if (shimInstalled) return true;

  const constants = zlib.constants ?? {};
  const quality = constants.BROTLI_PARAM_QUALITY ?? 5;

  if (typeof zlib.zstdCompress !== 'function') {
    zlib.zstdCompress = (buffer, _options, callback) => zlib.brotliCompress(buffer, { params: { [quality]: 5 } }, callback);
  }
  if (typeof zlib.zstdCompressSync !== 'function') {
    zlib.zstdCompressSync = (buffer) => zlib.brotliCompressSync(buffer);
  }
  if (typeof zlib.zstdDecompressSync !== 'function') {
    zlib.zstdDecompressSync = (buffer, options) => zlib.brotliDecompressSync(buffer, options);
  }
  if (typeof zlib.zstdDecompress !== 'function') {
    zlib.zstdDecompress = (buffer, options, callback) =>
      zlib.brotliDecompress(buffer, { maxOutputLength: options?.maxOutputLength }, callback);
  }

  shimInstalled = true;
  return true;
}

/** True when the runtime can actually talk to the TypeTube cluster. */
export function isTypeTubeRuntimeSupported() {
  return supportsZstd();
}

/**
 * Resolves and loads `typetube`. Cached: the module body runs once per process.
 * @returns {Promise<{ createClient: Function, TypeTubeError: Function }>}
 */
export async function loadTypeTube() {
  if (!isTypeTubeRuntimeSupported()) throw new TypeTubeUnsupportedRuntimeError();
  if (!modulePromise) {
    modulePromise = import('typetube').catch((error) => {
      modulePromise = null;
      throw error;
    });
  }
  return modulePromise;
}

/**
 * Returns a process-wide TypeTube client, creating it on first use.
 *
 * TypeTube has no cancellation API: when a job times out we abandon the promise and
 * drop the client so its sockets do not leak into the next job.
 *
 * @param {{ TYPETUBE_API_KEY?: string, TYPETUBE_HOST?: string, TYPETUBE_PORT?: number }} config
 */
export async function getTypeTubeClient(config = {}) {
  const wanted = {
    apiKey: config.TYPETUBE_API_KEY || undefined,
    host: config.TYPETUBE_HOST || undefined,
    port: config.TYPETUBE_PORT || undefined
  };

  const changed =
    client && lastClientConfig && (lastClientConfig.apiKey !== wanted.apiKey || lastClientConfig.host !== wanted.host);

  if (client && !changed) return client;

  if (client && changed) closeTypeTubeClient();

  const typeTube = await loadTypeTube();
  client = typeTube.createClient({
    ...(wanted.apiKey ? { apiKey: wanted.apiKey } : {}),
    ...(wanted.host ? { host: wanted.host } : {}),
    ...(wanted.port ? { port: wanted.port } : {})
  });
  lastClientConfig = wanted;
  return client;
}

/** Drops the cached client, closing its socket when it is still healthy. */
export function closeTypeTubeClient() {
  if (!client) return;
  try {
    client.close?.();
  } catch {
    // Closing a half-broken client is best effort.
  }
  client = null;
  lastClientConfig = null;
}

/** Lightweight capability report for /health. */
export function typeTubeRuntimeReport() {
  const supported = isTypeTubeRuntimeSupported();
  return {
    supported,
    nodeVersion: process.version,
    zstd: typeof zlib.zstdCompress === 'function' && !shimInstalled,
    shimInstalled,
    note: supported
      ? 'TypeTube is available as the primary downloader'
      : `TypeTube disabled: needs Node >= ${NODE_MIN_MAJOR}.${NODE_MIN_MINOR}`
  };
}

// Installed eagerly on import: if another module imports TypeTube before us, Node's
// module cache keeps zlib patched either way, but doing it here removes any doubt.
installZlibCompat();
