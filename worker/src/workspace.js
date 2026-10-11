/**
 * Temporary directory handling.
 *
 * A job gets its own directory under the OS temp dir and it is removed afterwards,
 * whatever happened. Container instances have small disks, so cleanup failures are
 * logged but never allowed to break the response to the user.
 */

import fs from 'node:fs';
import { promises as fsp, createReadStream } from 'node:fs';
import os from 'node:os';
import path from 'node:path';

const PREFIX = 'yt-worker-';

/** @returns {Promise<string>} an absolute path to a fresh directory */
export async function createJobDir() {
  return await fsp.mkdtemp(path.join(os.tmpdir(), PREFIX));
}

/** Best-effort recursive delete; retries once because Telegram may still hold a handle. */
export async function removeJobDir(dir, log) {
  if (!dir) return;
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await fsp.rm(dir, { recursive: true, force: true });
      return true;
    } catch (error) {
      if (attempt === 1) {
        log?.warn('Could not remove the job directory', { dir, message: error.message });
        return false;
      }
      await new Promise((resolve) => setTimeout(resolve, 250));
    }
  }
  return false;
}

/** Removes leftover directories from a previous run that was killed mid-job. */
export async function sweepStaleJobDirs(log, { olderThanMinutes = 180 } = {}) {
  const root = os.tmpdir();
  let entries = [];
  try {
    entries = await fsp.readdir(root, { withFileTypes: true });
  } catch {
    return 0;
  }

  const cutoff = Date.now() - olderThanMinutes * 60_000;
  let removed = 0;

  for (const entry of entries) {
    if (!entry.isDirectory() || !entry.name.startsWith(PREFIX)) continue;
    const target = path.join(root, entry.name);
    try {
      const stat = await fsp.stat(target);
      if (stat.mtimeMs > cutoff) continue;
      await fsp.rm(target, { recursive: true, force: true });
      removed += 1;
    } catch {
      // Another process may be using it; ignore.
    }
  }

  if (removed > 0) log?.info('Removed stale job directories', { removed });
  return removed;
}

export function fileSizeSync(target) {
  try {
    return fs.statSync(target).size;
  } catch {
    return 0;
  }
}

/** Free space in bytes on the filesystem holding `target`, or `null` when unknown. */
export async function freeSpaceBytes(target = os.tmpdir()) {
  try {
    const stat = await fsp.statfs(target);
    return Number(stat.bavail) * Number(stat.bsize);
  } catch {
    return null;
  }
}

/** Largest file in a directory matching one of the extensions. */
export async function largestFileWithExtension(dir, extensions) {
  const wanted = new Set(extensions.map((ext) => ext.toLowerCase()));
  const entries = await fsp.readdir(dir, { withFileTypes: true });
  const candidates = [];
  for (const entry of entries) {
    if (!entry.isFile()) continue;
    if (!wanted.has(path.extname(entry.name).toLowerCase())) continue;
    const target = path.join(dir, entry.name);
    candidates.push({ path: target, size: (await fsp.stat(target)).size });
  }
  candidates.sort((a, b) => b.size - a.size);
  return candidates[0] ?? null;
}

export async function writeStreamToFile(stream, target) {
  await fsp.mkdir(path.dirname(target), { recursive: true });
  await new Promise((resolve, reject) => {
    const out = fs.createWriteStream(target);
    stream.pipe(out);
    stream.on('error', reject);
    out.on('error', reject);
    out.on('finish', resolve);
  });
  return target;
}

export { createReadStream };
