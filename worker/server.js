import http from 'node:http';
import os from 'node:os';
import path from 'node:path';
import fs from 'node:fs';
import { promises as fsp } from 'node:fs';
import { execFile } from 'node:child_process';
import { promisify } from 'node:util';
import { tmpdir } from 'node:os';
import { randomUUID, timingSafeEqual } from 'node:crypto';
import axios from 'axios';
import FormData from 'form-data';
import { TypeTube, createClient } from 'typetube';

const execFileAsync = promisify(execFile);
const PORT = Number(process.env.PORT || 8080);
const BOT_TOKEN = (process.env.BOT_TOKEN || '').trim();
const WORKER_SECRET = (process.env.WORKER_SECRET || '').trim();
const MAX_CONCURRENT = Math.max(1, Number(process.env.MAX_CONCURRENT_DOWNLOADS || 2));
const MAX_UPLOAD_BYTES = 50_000_000;
const ALLOWED_QUALITIES = new Set(['360p', '480p', '720p', '1080p', '1440p', '2160p']);
const active = new Set();

function authorized(req) {
  const supplied = (req.headers.authorization || '').replace(/^Bearer\s+/i, '');
  if (!WORKER_SECRET || supplied.length !== WORKER_SECRET.length) return false;
  try {
    return timingSafeEqual(Buffer.from(supplied), Buffer.from(WORKER_SECRET));
  } catch {
    return false;
  }
}

function sendJson(res, status, payload) {
  res.writeHead(status, { 'content-type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

async function readJson(req) {
  let body = '';
  for await (const chunk of req) {
    body += chunk;
    if (body.length > 16_384) throw new Error('Request body too large');
  }
  return JSON.parse(body || '{}');
}

async function telegram(method, payload) {
  const response = await axios.post(`https://api.telegram.org/bot${BOT_TOKEN}/${method}`, payload);
  if (!response.data?.ok) throw new Error(response.data?.description || `Telegram ${method} failed`);
  return response.data.result;
}

async function editStatus(chatId, messageId, text) {
  try {
    await telegram('editMessageText', { chat_id: chatId, message_id: messageId, text });
  } catch (error) {
    console.warn('Unable to edit status message:', error.message);
  }
}

async function uploadVideo(filePath, chatId, caption) {
  const form = new FormData();
  form.append('chat_id', String(chatId));
  form.append('supports_streaming', 'true');
  form.append('caption', caption.slice(0, 900));
  form.append('video', fs.createReadStream(filePath), {
    filename: path.basename(filePath),
    contentType: 'video/mp4'
  });
  const response = await axios.post(`https://api.telegram.org/bot${BOT_TOKEN}/sendVideo`, form, {
    headers: form.getHeaders(),
    maxBodyLength: Infinity,
    maxContentLength: Infinity,
    timeout: 10 * 60 * 1000
  });
  if (!response.data?.ok) throw new Error(response.data?.description || 'Telegram upload failed');
  return response.data.result;
}

async function downloadWithTypeTube(url, quality, dir) {
  // TypeTube is the primary downloader. It requires a Node.js runtime, filesystem,
  // TCP access to its TAP cluster, and FFmpeg for muxing; do not import it in tgcloud/.
  const client = createClient();
  const track = await client.resolve(url, { maxVideoHeight: Number(quality.replace('p', '')) });
  const result = await client.downloadMedia(track, dir, {
    quality,
    outputFormat: 'mp4',
    preferCodec: 'avc1',
    workers: 4,
    onProgress: progress => {
      if (progress?.phase || progress?.percent) {
        console.log('TypeTube progress', progress.phase || 'download', progress.percent ?? '');
      }
    }
  });
  if (!result?.filePath) throw new Error('TypeTube did not return a media file path');
  return result.filePath;
}

async function downloadWithYtDlp(url, quality, dir) {
  // yt-dlp is intentionally a fallback, never the primary downloader.
  const output = path.join(dir, '%(title).100B-%(id)s.%(ext)s');
  await execFileAsync('yt-dlp', [
    '--no-playlist',
    '--no-warnings',
    '--restrict-filenames',
    '-f', `bv*[height<=${quality.replace('p', '')}]+ba/b[height<=${quality.replace('p', '')}]`,
    '--merge-output-format', 'mp4',
    '-o', output,
    url
  ], { timeout: 12 * 60 * 1000, maxBuffer: 4 * 1024 * 1024 });
  const candidates = (await fsp.readdir(dir))
    .filter(name => /\.(mp4|mkv|webm|mov)$/i.test(name))
    .map(name => path.join(dir, name));
  if (!candidates.length) throw new Error('yt-dlp fallback did not produce a video');
  const stats = await Promise.all(candidates.map(async filePath => ({ filePath, size: (await fsp.stat(filePath)).size })));
  stats.sort((a, b) => b.size - a.size);
  return stats[0].filePath;
}

async function processDownload(job) {
  const { url, quality, chat_id: chatId, status_message_id: statusMessageId } = job;
  const dir = await fsp.mkdtemp(path.join(tmpdir(), 'typetube-bot-'));
  try {
    await editStatus(chatId, statusMessageId, '🔎 Resolving video with TypeTube…');
    let filePath;
    try {
      filePath = await downloadWithTypeTube(url, quality, dir);
      console.log('Download succeeded with TypeTube');
    } catch (error) {
      console.warn('TypeTube failed; trying yt-dlp fallback:', error.message);
      await editStatus(chatId, statusMessageId, '⚠️ TypeTube failed. Trying yt-dlp fallback…');
      // Use a separate directory so partially downloaded TypeTube files are ignored.
      const fallbackDir = path.join(dir, 'fallback');
      await fsp.mkdir(fallbackDir, { recursive: true });
      filePath = await downloadWithYtDlp(url, quality, fallbackDir);
    }

    const stat = await fsp.stat(filePath);
    if (stat.size > MAX_UPLOAD_BYTES) {
      await editStatus(chatId, statusMessageId,
        `⚠️ Download finished (${(stat.size / 1_000_000).toFixed(1)} MB), but Telegram's hosted Bot API upload limit is 50 MB. Retry with /download 480p <URL> or /download 360p <URL>.`);
      return;
    }

    await editStatus(chatId, statusMessageId, `⬆️ Uploading ${path.basename(filePath)} (${(stat.size / 1_000_000).toFixed(1)} MB)…`);
    await uploadVideo(filePath, chatId, `🎬 ${path.basename(filePath).replace(/\.[^.]+$/, '')}`);
    try {
      await telegram('deleteMessage', { chat_id: chatId, message_id: statusMessageId });
    } catch {}
  } catch (error) {
    console.error('Download job failed:', error);
    await editStatus(chatId, statusMessageId, `❌ Download failed: ${String(error.message || error).slice(0, 700)}`);
  } finally {
    await fsp.rm(dir, { recursive: true, force: true }).catch(() => {});
    active.delete(job.chat_id);
  }
}

const server = http.createServer(async (req, res) => {
  if (req.method === 'GET' && (req.url === '/' || req.url === '/health')) {
    return sendJson(res, 200, { ok: true, service: 'typetube-download-worker', active: active.size });
  }
  if (req.method !== 'POST' || req.url !== '/download') return sendJson(res, 404, { error: 'Not found' });
  if (!authorized(req)) return sendJson(res, 401, { error: 'Unauthorized' });
  let job;
  try {
    job = await readJson(req);
    const parsed = new URL(job.url);
    const allowed = ['youtube.com', 'www.youtube.com', 'm.youtube.com', 'music.youtube.com', 'youtu.be', 'www.youtu.be'];
    if (!['http:', 'https:'].includes(parsed.protocol) || !allowed.includes(parsed.hostname.toLowerCase()) ||
        !job.chat_id || !job.status_message_id || !ALLOWED_QUALITIES.has(job.quality)) {
      return sendJson(res, 400, { error: 'Invalid YouTube URL, chat ID, status message, or quality' });
    }
    if (active.has(job.chat_id)) return sendJson(res, 429, { error: 'This chat already has an active download' });
    if (active.size >= MAX_CONCURRENT) return sendJson(res, 503, { error: 'Worker is busy; retry shortly' });
    active.add(job.chat_id);
    res.writeHead(202, { 'content-type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ ok: true, queued: true }));
    void processDownload(job);
  } catch (error) {
    console.error('Request rejected:', error.message);
    if (!res.headersSent) sendJson(res, 400, { error: 'Invalid JSON request' });
  }
});

if (!BOT_TOKEN || !WORKER_SECRET) {
  console.error('BOT_TOKEN and WORKER_SECRET environment variables are required');
  process.exit(1);
}
server.listen(PORT, '0.0.0.0', () => console.log(`TypeTube worker listening on ${PORT} (os=${os.platform()})`));
