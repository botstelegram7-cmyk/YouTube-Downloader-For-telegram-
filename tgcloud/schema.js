/**
 * Database schema for the Serverless bot.
 *
 * Deploying this file and running `npx tgcloud migrate` creates the tables. Rules
 * that shape it (see the Serverless SDK reference):
 *   - foreign keys are not supported and throw when declared, so integrity is
 *     enforced in application code;
 *   - columns are only dropped through `.deprecated('reason')`;
 *   - every table is a named export and everything lives in this one file.
 */

import { table, integer, text, index, sql } from 'sdk/db';

/** One row per user: their default quality and delivery mode. */
export const userPreferences = table('user_preferences', {
  userId: integer('user_id').primaryKey(),
  quality: text('quality').notNull().default('720p'),
  mode: text('mode').notNull().default('video'),
  updatedAt: integer('updated_at', { mode: 'timestamp' }).default(sql`(unixepoch())`)
});

/**
 * Key/value configuration. This is where the worker URL and shared secret live:
 * the Serverless runtime exposes no environment variables or secret store, and
 * keeping them in the database means no secret is ever committed to git.
 */
export const botSettings = table('bot_settings', {
  key: text('key').primaryKey(),
  value: text('value').notNull(),
  updatedAt: integer('updated_at', { mode: 'timestamp' }).default(sql`(unixepoch())`)
});

/**
 * One row per submitted download. Used for the daily quota, for the Cancel button,
 * and for the "retry smaller" buttons after a file is rejected for being too large.
 */
export const downloadJobs = table(
  'download_jobs',
  {
    id: text('id').primaryKey(),
    userId: integer('user_id').notNull(),
    chatId: integer('chat_id').notNull(),
    statusMessageId: integer('status_message_id'),
    sourceUrl: text('source_url').notNull(),
    quality: text('quality').notNull(),
    mode: text('mode').notNull().default('video'),
    status: text('status').notNull().default('queued'),
    createdAt: integer('created_at', { mode: 'timestamp' }).default(sql`(unixepoch())`),
    finishedAt: integer('finished_at', { mode: 'timestamp' })
  },
  (t) => ({
    userCreatedIdx: index('idx_download_jobs_user_created').on(t.userId, t.createdAt),
    chatStatusIdx: index('idx_download_jobs_chat_status').on(t.chatId, t.status)
  })
);
