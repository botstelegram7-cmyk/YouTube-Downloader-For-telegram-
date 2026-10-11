/**
 * Minimal Telegram Bot API client.
 *
 * Two transports are needed:
 *  - JSON calls (editMessageText, deleteMessage, sendChatAction, getMe) via global fetch.
 *  - multipart uploads (sendVideo, sendAudio, sendDocument) via `form-data` streamed
 *    over node:https, so a 50 MB file never has to be held in memory.
 *
 * Everything the Bot API can do wrong is normalised into `TelegramApiError` with the
 * `.code` from the API, and 429s are retried using the server-provided `retry_after`.
 */

import fs from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import FormData from 'form-data';

const DEFAULT_API_BASE = 'https://api.telegram.org';

export class TelegramApiError extends Error {
  constructor(method, description, { code = 0, parameters = null, cause = null } = {}) {
    super(`Telegram ${method} failed (${code || 'network'}): ${description}`);
    this.name = 'TelegramApiError';
    this.method = method;
    this.code = code;
    this.description = description;
    this.parameters = parameters;
    if (cause) this.cause = cause;
  }

  /** Telegram's own text for "the file is larger than the upload limit". */
  get isTooLarge() {
    return /too big|too large|file size|entity too large|413/i.test(this.description ?? '');
  }

  /** The status text was unchanged — harmless, never worth surfacing. */
  get isNotModified() {
    return /message is not modified/i.test(this.description ?? '');
  }

  get isRateLimited() {
    return this.code === 429;
  }

  get retryAfterSeconds() {
    const value = Number(this.parameters?.retry_after);
    return Number.isFinite(value) && value > 0 ? value : 3;
  }
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

export class TelegramClient {
  /**
   * @param {object} options
   * @param {string} options.token BotFather bot token.
   * @param {import('./logger.js').Logger} [options.log]
   * @param {number} [options.retries] Retries for 429/5xx responses.
   */
  constructor({ token, log, retries = 3, apiBase = DEFAULT_API_BASE }) {
    if (!token) throw new Error('TelegramClient requires a bot token');
    this.token = token;
    this.log = log;
    this.retries = retries;
    // Pointing this at a self-hosted Bot API server lifts the 50 MB upload cap;
    // the local mock server used by scripts/e2e_local.mjs relies on the same switch.
    this.apiBase = String(apiBase || DEFAULT_API_BASE).replace(/\/+$/, '');
    this.botInfo = null;
  }

  endpoint(method) {
    return `${this.apiBase}/bot${this.token}/${method}`;
  }

  /**
   * Uploads use node:http(s) directly instead of fetch, so the file streams instead of
   * being buffered in memory. The transport has to follow the base URL: a self-hosted
   * Bot API server normally listens on plain http://127.0.0.1.
   */
  transport(method) {
    return this.endpoint(method).startsWith('http://') ? http : https;
  }

  /** JSON API call with retry/backoff. Resolves to the unwrapped `result`. */
  async call(method, params = {}, { timeoutMs = 20_000 } = {}) {
    let lastError = null;

    for (let attempt = 0; attempt <= this.retries; attempt += 1) {
      try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), timeoutMs);
        let response;
        try {
          response = await fetch(this.endpoint(method), {
            method: 'POST',
            headers: { 'content-type': 'application/json' },
            body: JSON.stringify(params),
            signal: controller.signal
          });
        } finally {
          clearTimeout(timer);
        }

        const payload = await response.json().catch(() => null);

        if (payload?.ok) return payload.result;

        const error = new TelegramApiError(method, payload?.description ?? `HTTP ${response.status}`, {
          code: payload?.error_code ?? response.status,
          parameters: payload?.parameters ?? null
        });

        if (error.isNotModified) return null;

        if (error.isRateLimited && attempt < this.retries) {
          this.log?.warn('Telegram rate limit, backing off', { method, retryAfter: error.retryAfterSeconds });
          await sleep(Math.min(error.retryAfterSeconds, 30) * 1000);
          lastError = error;
          continue;
        }

        throw error;
      } catch (error) {
        if (error instanceof TelegramApiError) throw error;

        lastError = new TelegramApiError(method, error?.message ?? 'network error', { cause: error });
        const retryable = attempt < this.retries;
        if (!retryable) throw lastError;
        await sleep(500 * 2 ** attempt);
      }
    }

    throw lastError ?? new TelegramApiError(method, 'unreachable');
  }

  async getMe() {
    this.botInfo ??= await this.call('getMe', {}, { timeoutMs: 10_000 });
    return this.botInfo;
  }

  /**
   * Edits a status message, silently ignoring "message is not modified" and
   * messages the user already deleted.
   * @returns {Promise<boolean>} true when the text was updated
   */
  async editStatus(chatId, messageId, text, { disablePreview = true } = {}) {
    try {
      await this.call('editMessageText', {
        chat_id: chatId,
        message_id: messageId,
        text,
        ...(disablePreview ? { link_preview_options: { is_disabled: true } } : {})
      });
      return true;
    } catch (error) {
      if (error instanceof TelegramApiError && (error.isNotModified || error.code === 400)) return false;
      throw error;
    }
  }

  async sendChatAction(chatId, action) {
    try {
      await this.call('sendChatAction', { chat_id: chatId, action }, { timeoutMs: 10_000 });
      return true;
    } catch {
      return false; // Cosmetic only.
    }
  }

  async deleteMessage(chatId, messageId) {
    try {
      await this.call('deleteMessage', { chat_id: chatId, message_id: messageId }, { timeoutMs: 10_000 });
      return true;
    } catch {
      return false;
    }
  }

  /**
   * Uploads a file with a streamed multipart body.
   *
   * @param {string} method Bot API method (sendVideo, sendAudio, sendDocument…)
   * @param {object} fields Scalar fields (chat_id, caption, duration…)
   * @param {{ path: string, field: string, filename: string, contentType: string }|null} file
   * @param {{ signal?: AbortSignal, timeoutMs?: number, extraFiles?: Array<{path:string,field:string,filename:string,contentType:string}> }} [options]
   */
  async upload(method, fields, file, { signal, timeoutMs = 900_000, extraFiles = [] } = {}) {
    const form = new FormData();
    for (const [key, value] of Object.entries(fields)) {
      if (value === undefined || value === null) continue;
      if (typeof value === 'object') {
        form.append(key, JSON.stringify(value));
      } else {
        form.append(key, String(value));
      }
    }

    const files = file ? [file, ...extraFiles] : extraFiles;
    for (const item of files) {
      const size = fs.statSync(item.path).size;
      form.append(item.field, fs.createReadStream(item.path), {
        filename: item.filename,
        contentType: item.contentType,
        knownLength: size
      });
    }

    const length = form.getLengthSync();

    return await new Promise((resolve, reject) => {
      const request = this.transport(method).request(
        this.endpoint(method),
        {
          method: 'POST',
          headers: { ...form.getHeaders(), 'content-length': length },
          timeout: timeoutMs
        },
        (response) => {
          const chunks = [];
          response.on('data', (chunk) => chunks.push(chunk));
          response.on('end', () => {
            const raw = Buffer.concat(chunks).toString('utf8');
            let payload = null;
            try {
              payload = JSON.parse(raw);
            } catch {
              // Fall through to a generic error below.
            }
            if (payload?.ok) {
              resolve(payload.result);
              return;
            }
            reject(
              new TelegramApiError(method, payload?.description ?? raw.slice(0, 300) ?? 'empty response', {
                code: payload?.error_code ?? response.statusCode
              })
            );
          });
        }
      );

      const abort = () => {
        request.destroy(new TelegramApiError(method, 'upload aborted', { code: 499 }));
      };
      signal?.addEventListener('abort', abort, { once: true });

      request.on('error', (error) => {
        signal?.removeEventListener('abort', abort);
        reject(error instanceof TelegramApiError ? error : new TelegramApiError(method, error.message, { cause: error }));
      });

      request.on('timeout', () => {
        request.destroy(new TelegramApiError(method, `upload timed out after ${Math.round(timeoutMs / 1000)}s`, { code: 408 }));
      });

      form.on('error', (error) => {
        request.destroy(new TelegramApiError(method, `multipart stream failed: ${error.message}`, { cause: error }));
      });

      form.pipe(request);
    });
  }

  /**
   * Delivers a video with a graceful degradation path:
   *   sendVideo (+ thumbnail) → sendVideo (no thumbnail) → sendDocument.
   * Each fallback keeps the user's file even when Telegram dislikes the codec.
   *
   * @returns {Promise<{ method: string, result: object }>}
   */
  async sendVideoFile({ chatId, filePath, caption, duration, width, height, thumbnailPath, signal }) {
    const base = {
      chat_id: chatId,
      caption,
      supports_streaming: true,
      duration: duration ? Math.round(duration) : undefined,
      width: width || undefined,
      height: height || undefined
    };

    const attempts = [
      {
        method: 'sendVideo',
        fields: base,
        file: { path: filePath, field: 'video', filename: 'video.mp4', contentType: 'video/mp4' },
        extraFiles: thumbnailPath
          ? [{ path: thumbnailPath, field: 'thumbnail', filename: 'thumb.jpg', contentType: 'image/jpeg' }]
          : []
      },
      {
        method: 'sendVideo',
        fields: base,
        file: { path: filePath, field: 'video', filename: 'video.mp4', contentType: 'video/mp4' },
        extraFiles: []
      },
      {
        method: 'sendDocument',
        fields: { chat_id: chatId, caption, disable_content_type_detection: true },
        file: { path: filePath, field: 'document', filename: 'video.mp4', contentType: 'video/mp4' },
        extraFiles: []
      }
    ];

    let lastError = null;
    for (const attempt of attempts) {
      try {
        const result = await this.upload(attempt.method, attempt.fields, attempt.file, {
          signal,
          extraFiles: attempt.extraFiles
        });
        return { method: attempt.method, result };
      } catch (error) {
        if (error instanceof TelegramApiError && error.isTooLarge) throw error; // No fallback helps.
        if (signal?.aborted) throw error;
        lastError = error;
        this.log?.warn('Upload attempt failed, trying a simpler form', {
          method: attempt.method,
          description: error?.description ?? error?.message
        });
      }
    }
    throw lastError;
  }

  /**
   * Sends a file as a document. Used for audio the Bot API would not accept as music
   * (for example Opus/WebM from a worker without FFmpeg), where calling it a video
   * would be a lie.
   */
  async sendDocumentFile({ chatId, filePath, caption, filename, contentType = 'application/octet-stream', signal }) {
    const result = await this.upload(
      'sendDocument',
      { chat_id: chatId, caption },
      { path: filePath, field: 'document', filename, contentType },
      { signal }
    );
    return { method: 'sendDocument', result };
  }

  /** Uploads audio with title/performer metadata; falls back to a document. */
  async sendAudioFile({ chatId, filePath, title, performer, duration, caption, signal }) {
    const attempts = [
      {
        method: 'sendAudio',
        fields: {
          chat_id: chatId,
          caption,
          title: title?.slice(0, 64),
          performer: performer?.slice(0, 64),
          duration: duration ? Math.round(duration) : undefined
        },
        file: { path: filePath, field: 'audio', filename: 'audio.m4a', contentType: 'audio/mp4' }
      },
      {
        method: 'sendDocument',
        fields: { chat_id: chatId, caption },
        file: { path: filePath, field: 'document', filename: 'audio.m4a', contentType: 'audio/mp4' }
      }
    ];

    let lastError = null;
    for (const attempt of attempts) {
      try {
        const result = await this.upload(attempt.method, attempt.fields, attempt.file, { signal });
        return { method: attempt.method, result };
      } catch (error) {
        if (error instanceof TelegramApiError && error.isTooLarge) throw error;
        if (signal?.aborted) throw error;
        lastError = error;
      }
    }
    throw lastError;
  }
}
