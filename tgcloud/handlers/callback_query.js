/**
 * callback_query handler — the inline buttons under a status message.
 *
 * Serverless contract: `input` is the CallbackQuery (not the Update), and `api`
 * answers events. Every branch ends with `answerCallbackQuery`, otherwise the little
 * spinner in the client keeps turning until it times out.
 *
 * Buttons:
 *   q:<quality>            pick a default quality
 *   cancel:<jobId>         stop a running download
 *   dl:<quality>:<jobId>   retry the same link at a smaller quality
 *   help | status          shortcuts
 */

import { api } from 'sdk';
import { CONFIG } from '../lib/config.js';
import { decodeCallbackData } from '../lib/youtube.js';
import * as M from '../lib/messages.js';
import { editMessage, sendMessage, startDownload, stopDownload } from '../lib/download_flow.js';
import { getJob, getPreferences, resolveOwnerId, savePreferences, updateJobStatus } from '../lib/storage.js';

export default async function handleCallbackQuery(query) {
  if (!query?.id) return;

  const chatId = query.message?.chat?.id;
  const messageId = query.message?.message_id;
  const userId = query.from?.id ?? 0;
  const data = decodeCallbackData(query.data);

  if (!data) {
    await answer(query.id, 'That button is no longer valid.');
    return;
  }

  switch (data.action) {
    case 'quality':
      await handleQualityChoice({ query, chatId, messageId, userId, quality: data.quality });
      return;

    case 'cancel':
      await handleCancel({ query, chatId, userId, jobId: data.jobId });
      return;

    case 'download':
      await handleRetry({ query, chatId, userId, quality: data.quality, jobId: data.jobId });
      return;

    case 'status':
      await answer(query.id);
      await sendMessage(chatId, 'Use /status for worker health and your running job.');
      return;

    case 'help':
      await answer(query.id);
      await sendMessage(chatId, M.helpText({
        config: CONFIG,
        quality: (await getPreferences(userId)).quality,
        mode: (await getPreferences(userId)).mode,
        configured: true,
        isOwner: (await resolveOwnerId(CONFIG.OWNER_USER_ID)) === userId
      }));
      return;

    default:
      await answer(query.id, 'Unsupported action.');
  }
}

async function answer(callbackQueryId, text) {
  try {
    await api.answerCallbackQuery({ callback_query_id: callbackQueryId, ...(text ? { text } : {}) });
  } catch (error) {
    console.log('answerCallbackQuery failed', { message: error?.description ?? error?.message });
  }
}

async function handleQualityChoice({ query, chatId, messageId, userId, quality }) {
  const saved = await savePreferences(userId, { quality });
  await answer(query.id, `Quality set to ${quality}`);
  if (messageId) await editMessage(chatId, messageId, M.qualitySaved(saved.quality, saved.mode));
}

async function handleCancel({ query, chatId, userId, jobId }) {
  const result = await stopDownload({ jobId, userId, config: CONFIG });
  await answer(query.id, result.ok ? 'Cancelling…' : result.message.replace(/<[^>]+>/g, ''));

  if (!result.ok && !result.message.includes('<b>Cancel')) {
    await sendMessage(chatId, result.message);
  }
}

async function handleRetry({ query, chatId, userId, quality, jobId }) {
  const job = await getJob(jobId);

  if (!job) {
    await answer(query.id, 'That download is no longer in the database.');
    return;
  }
  if (job.userId !== userId) {
    await answer(query.id, 'Only the person who started it can retry.');
    return;
  }

  await answer(query.id, `Retrying at ${quality}…`);

  // The worker only offers this button after it gave up on the file, so the job it
  // refers to is finished: close the row or the quota check would refuse the retry.
  await updateJobStatus(jobId, 'retried');

  const result = await startDownload({
    chatId,
    userId,
    url: job.sourceUrl,
    quality,
    mode: job.mode ?? 'video',
    config: CONFIG
  });

  if (!result.ok && !result.reported) await sendMessage(chatId, result.message);
}
