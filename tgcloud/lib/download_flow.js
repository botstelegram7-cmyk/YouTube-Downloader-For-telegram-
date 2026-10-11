/**
 * The one path every download takes — used by the message handler and by the
 * inline buttons (retry smaller / cancel), so behaviour cannot drift between them.
 *
 * Order of operations is deliberate:
 *   1. refuse early (no worker configured, cooldown, daily cap, already running);
 *   2. create the status message so the user sees something immediately;
 *   3. record the job row, then hand it to the worker;
 *   4. tell the worker our own status message id, so it can edit that message while
 *      it downloads and uploads.
 */

import { api } from 'sdk';
import { CONFIG } from './config.js';
import { encodeCallbackData } from './youtube.js';
import {
  alreadyRunning,
  busy,
  cancelled,
  cooldown,
  dailyLimitReached,
  notConfigured,
  nothingToCancel,
  queued,
  workerError
} from './messages.js';
import { cancelJob, submitJob, WorkerError } from './worker_api.js';
import {
  closeStaleJobs,
  countJobsToday,
  getActiveJob,
  getJob,
  getLastJob,
  insertJob,
  resolveWorkerConfig,
  updateJobStatus
} from './storage.js';

/**
 * A job row only tells us that a submission was accepted — the worker owns the live
 * state, and it cannot write back into the bot's database (the platform exposes no
 * inbound webhook for our own code). So the window is deliberately as short as the
 * cooldown: it exists to absorb double-taps, while the worker's own 409 is the
 * authoritative answer for "is something already running" and /status asks the worker
 * for the truth.
 */
const ACTIVE_JOB_WINDOW_SECONDS = CONFIG.COOLDOWN_SECONDS;

export function makeJobId() {
  // Short, URL-safe and unique enough: the platform has no crypto module.
  return `j${Date.now().toString(36)}${Math.random().toString(36).slice(2, 8)}`;
}

export async function sendMessage(chatId, text, extra = {}) {
  return await api.sendMessage({
    chat_id: chatId,
    text,
    parse_mode: 'HTML',
    link_preview_options: { is_disabled: true },
    ...extra
  });
}

export async function editMessage(chatId, messageId, text, extra = {}) {
  try {
    return await api.editMessageText({
      chat_id: chatId,
      message_id: messageId,
      text,
      parse_mode: 'HTML',
      link_preview_options: { is_disabled: true },
      ...extra
    });
  } catch (error) {
    // "message is not modified" and deleted messages are both fine here.
    console.log('editMessage ignored a Telegram error', { messageId, message: error?.description ?? error?.message });
    return null;
  }
}

/** Cools down an abusive or stuck user before any work is scheduled. */
export async function checkQuota(userId) {
  const active = await getActiveJob(userId);
  if (active) {
    const ageSeconds = ageInSeconds(active.createdAt);
    if (ageSeconds <= ACTIVE_JOB_WINDOW_SECONDS) return { ok: false, message: alreadyRunning(), activeJob: active };
    // Older than the window: the status is stale, so stop blocking this user.
    await updateJobStatus(active.id, 'expired');
  }

  const last = await getLastJob(userId);
  if (last && last.status === 'queued') {
    const elapsed = ageInSeconds(last.createdAt);
    if (elapsed < CONFIG.COOLDOWN_SECONDS) {
      return { ok: false, message: cooldown(Math.max(1, Math.ceil(CONFIG.COOLDOWN_SECONDS - elapsed))) };
    }
  }

  const usedToday = await countJobsToday(userId);
  if (usedToday >= CONFIG.MAX_JOBS_PER_DAY) {
    return { ok: false, message: dailyLimitReached(CONFIG.MAX_JOBS_PER_DAY) };
  }

  return { ok: true };
}

function ageInSeconds(createdAt) {
  const time = createdAt instanceof Date ? createdAt.getTime() : new Date(createdAt ?? Date.now()).getTime();
  return Math.max(0, (Date.now() - time) / 1000);
}

export function cancelKeyboard(jobId) {
  return { inline_keyboard: [[{ text: '🛑 Cancel', callback_data: encodeCallbackData(['cancel', jobId]) }]] };
}

/**
 * Submits a download. Always resolves with `{ ok, message, jobId? }`; it never throws
 * at the caller, because every failure has a user-facing explanation.
 */
export async function startDownload({ chatId, userId, url, quality, mode, replyToMessageId, config = CONFIG }) {
  const worker = await resolveWorkerConfig(config);
  // `reported: false` tells the caller that nothing was shown to the user yet.
  if (!worker) return { ok: false, message: notConfigured(), reported: false };

  const quota = await checkQuota(userId);
  if (!quota.ok) return { ...quota, reported: false };

  const jobId = makeJobId();

  const statusMessage = await sendMessage(chatId, '⏳ <b>Contacting the worker…</b>', {
    ...(replyToMessageId ? { reply_to_message_id: replyToMessageId } : {})
  });
  const statusMessageId = statusMessage?.message_id ?? null;

  await insertJob({
    id: jobId,
    userId,
    chatId,
    statusMessageId,
    sourceUrl: url,
    quality,
    mode,
    status: 'queued'
  });

  try {
    const response = await submitJob({
      workerUrl: worker.url,
      workerSecret: worker.secret,
      config,
      payload: {
        job_id: jobId,
        url,
        chat_id: chatId,
        user_id: userId,
        status_message_id: statusMessageId,
        quality,
        mode
      }
    });

    const position = Number(response?.position ?? 0);
    await editMessage(
      chatId,
      statusMessageId,
      queued({ position, quality, mode, etaHint: position > 1 ? 'The queue moves as soon as the current job ends.' : null }),
      { reply_markup: cancelKeyboard(jobId) }
    );

    return { ok: true, jobId, position, reported: true };
  } catch (error) {
    const kind = error instanceof WorkerError ? error.kind : 'unknown';
    console.log('Worker submission failed', { jobId, kind, message: error?.message });

    await updateJobStatus(
      jobId,
      kind === 'unauthorized' ? 'failed_auth' : kind === 'duplicate' ? 'running' : 'failed_submit'
    );
    const text = kind === 'busy' ? busy() : workerError(error instanceof WorkerError ? error : { kind: 'unknown' });
    await editMessage(chatId, statusMessageId, text);

    return { ok: false, message: text, jobId, reported: true };
  }
}

/** Cancels a job: asks the worker, then closes the local row. */
export async function stopDownload({ jobId, userId, config = CONFIG }) {
  const job = await getJob(jobId);
  if (!job || job.userId !== userId) {
    await closeStaleJobs(userId);
    return { ok: false, message: nothingToCancel() };
  }

  if (job.status !== 'queued') return { ok: false, message: nothingToCancel() };

  const worker = await resolveWorkerConfig(config);
  if (worker) {
    await cancelJob({ workerUrl: worker.url, workerSecret: worker.secret, jobId });
  }
  await updateJobStatus(jobId, 'cancelled');

  if (job.statusMessageId) {
    await editMessage(job.chatId, job.statusMessageId, cancelled());
  }

  return { ok: true, message: cancelled() };
}
