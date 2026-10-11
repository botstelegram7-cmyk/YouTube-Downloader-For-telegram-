/**
 * Database access for the bot.
 *
 * Everything here is thin on purpose: the SQL lives in the builders below, and the
 * handlers stay about conversation logic. Two rules from the Serverless SDK apply
 * throughout — every call is awaited (a forgotten await returns a builder, not rows),
 * and there are no foreign keys, so referential integrity is handled in this file.
 *
 * The schema is also available as a local stub (tests/stubs/sdk.js) so the handlers
 * can be tested with `node --import tests/stubs/register.mjs` outside Telegram.
 */

import { db } from 'sdk';
import { and, desc, eq, gt } from 'sdk/db';
import { botSettings, downloadJobs, userPreferences } from '../schema.js';
import { DEFAULT_MODE, DEFAULT_QUALITY } from './youtube.js';

/** Reads every settings row at once; the table is tiny. */
export async function readSettings() {
  const rows = await db.select().from(botSettings).all();
  const map = {};
  for (const row of rows ?? []) map[row.key] = row.value;
  return map;
}

export async function getSetting(key) {
  const row = await db.select().from(botSettings).where(eq(botSettings.key, key)).get();
  return row ? row.value : null;
}

/** Creates or updates a setting. Replaces onConflict with an explicit two-step write. */
export async function setSetting(key, value) {
  const existing = await getSetting(key);
  if (existing === null) {
    await db
      .insert(botSettings)
      .values({ key, value: String(value) })
      .run();
    return;
  }
  await db
    .update(botSettings)
    .set({ value: String(value) })
    .where(eq(botSettings.key, key))
    .run();
}

export async function deleteSetting(key) {
  await db.delete(botSettings).where(eq(botSettings.key, key)).run();
}

/**
 * Resolves the worker connection: database first, then the optional values compiled
 * into lib/config.js. Returns null when the bot has never been configured.
 */
export async function resolveWorkerConfig(compileTime = {}) {
  const settings = await readSettings();
  const url = settings.worker_url || compileTime.WORKER_URL || '';
  const secret = settings.worker_secret || compileTime.WORKER_SECRET || '';
  if (!url || !secret) return null;
  return { url, secret, source: settings.worker_url ? 'database' : 'config file' };
}

/** The owner is either configured explicitly or claimed by the first /setup. */
export async function resolveOwnerId(configuredOwnerId = 0) {
  if (configuredOwnerId && Number(configuredOwnerId) > 0) return Number(configuredOwnerId);
  const claimed = await getSetting('owner_user_id');
  return claimed ? Number(claimed) : 0;
}

export async function claimOwnership(userId) {
  await setSetting('owner_user_id', String(userId));
}

export async function getPreferences(userId) {
  const row = await db.select().from(userPreferences).where(eq(userPreferences.userId, userId)).get();
  return {
    quality: row?.quality ?? DEFAULT_QUALITY,
    mode: row?.mode ?? DEFAULT_MODE
  };
}

export async function savePreferences(userId, patch) {
  const current = await getPreferences(userId);
  const next = { ...current, ...patch };
  const existing = await db.select().from(userPreferences).where(eq(userPreferences.userId, userId)).get();

  if (!existing) {
    await db.insert(userPreferences).values({ userId, quality: next.quality, mode: next.mode }).run();
  } else {
    await db
      .update(userPreferences)
      .set({ quality: next.quality, mode: next.mode })
      .where(eq(userPreferences.userId, userId))
      .run();
  }

  return next;
}

export async function insertJob(job) {
  await db
    .insert(downloadJobs)
    .values({
      id: job.id,
      userId: job.userId,
      chatId: job.chatId,
      statusMessageId: job.statusMessageId ?? null,
      sourceUrl: job.sourceUrl,
      quality: job.quality,
      mode: job.mode,
      status: job.status ?? 'queued'
    })
    .run();
}

export async function getJob(jobId) {
  return (await db.select().from(downloadJobs).where(eq(downloadJobs.id, jobId)).get()) ?? null;
}

export async function updateJobStatus(jobId, status) {
  await db
    .update(downloadJobs)
    .set({ status, finishedAt: new Date() })
    .where(eq(downloadJobs.id, jobId))
    .run();
}

/** Jobs still believed to be running for this user, newest first. */
export async function getActiveJob(userId) {
  const job = await db
    .select()
    .from(downloadJobs)
    .where(and(eq(downloadJobs.userId, userId), eq(downloadJobs.status, 'queued')))
    .orderBy(desc(downloadJobs.createdAt))
    .limit(1)
    .get();
  return job ?? null;
}

/**
 * Counting is done with a bounded window instead of SQL date functions, so it works
 * identically on the platform's SQLite and on the local test stub.
 */
export async function countJobsSince(userId, secondsAgo) {
  const since = new Date(Date.now() - secondsAgo * 1000);
  const rows = await db
    .select()
    .from(downloadJobs)
    .where(and(eq(downloadJobs.userId, userId), gt(downloadJobs.createdAt, since)))
    .all();
  return (rows ?? []).length;
}

export async function countJobsToday(userId) {
  return await countJobsSince(userId, 24 * 60 * 60);
}

/** Last submission time for the per-user cooldown. */
export async function getLastJob(userId) {
  const job = await db
    .select()
    .from(downloadJobs)
    .where(eq(downloadJobs.userId, userId))
    .orderBy(desc(downloadJobs.createdAt))
    .limit(1)
    .get();
  return job ?? null;
}

/** Marks every stale 'queued' row as done so the UI is not blocked forever. */
export async function closeStaleJobs(userId) {
  const stale = await db
    .select()
    .from(downloadJobs)
    .where(and(eq(downloadJobs.userId, userId), eq(downloadJobs.status, 'queued')))
    .all();
  for (const job of stale ?? []) await updateJobStatus(job.id, 'expired');
  return (stale ?? []).length;
}
