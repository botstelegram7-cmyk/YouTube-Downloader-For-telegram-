# Architecture

## Why there are two pieces

Telegram Serverless runs your bot's modules in a **V8 sandbox**: JavaScript only, with a
SQLite database, the Bot API and outbound HTTP. It has no filesystem, no npm packages,
no TCP sockets and no child processes.

TypeTube needs all four of those things (a persistent socket to its cluster, temp files,
FFmpeg, and range downloads). So the work is split:

| Concern | Runs on Telegram Serverless | Runs on the Render worker |
|---|---|---|
| Receiving updates | ✓ | |
| Commands, buttons, preferences | ✓ | |
| User-visible status messages | ✓ (creates them) | ✓ (edits them) |
| Rate limits, quotas, ownership | ✓ | |
| Resolving the video | | ✓ |
| Downloading bytes | | ✓ |
| Muxing with FFmpeg | | ✓ |
| Uploading to Telegram | | ✓ |
| Storing the worker secret | ✓ (database) | |

The handler never touches video data, so it always answers within milliseconds — which
matters, because an update handler that blocks for minutes would be killed.

## Components

### Serverless side (`tgcloud/`)

* **`handlers/message.js`** — every command and every bare link. Owns the conversation.
* **`handlers/callback_query.js`** — inline buttons: quality picker, Cancel, retry
  smaller, help.
* **`lib/download_flow.js`** — the single path a download takes. Quota checks, status
  message creation, job recording, submission, error copy. Both handlers call it, so
  behaviour cannot drift.
* **`lib/worker_api.js`** — the authenticated client. Pure helpers (classification,
  masking, backoff) are separated from I/O so they can be unit-tested outside Telegram.
* **`lib/storage.js`** — all database access. Two platform rules are honoured here: no
  foreign keys (integrity is enforced in application code) and every call is awaited.
* **`lib/youtube.js`** — URL/command parsing, deliberately dependency-free so it loads
  in the sandbox and in `node --test`.
* **`lib/messages.js`** — every user-facing string, HTML-escaped, in one place.

### Worker side (`worker/`)

* **`server.js`** — four routes, one shared secret, constant-time comparison, strict
  payload validation.
* **`queue.js`** — concurrency limit, one job per chat *and* per user, bounded waiting
  list, `AbortController` per job.
* **`downloader.js`** — TypeTube first; yt-dlp when TypeTube cannot deliver; a downgrade
  ladder when the result will not fit; normalised results so the uploader does not care
  which engine won.
* **`typetube.js`** — the runtime guard described in
  [TYPETUBE_COMPAT.md](TYPETUBE_COMPAT.md).
* **`telegram.js`** — Bot API client. JSON calls go through `fetch`; uploads stream
  multipart bodies with `form-data` over `node:https`, so a 50 MB file is never held in
  memory. `sendVideo` degrades to `sendVideo`-without-thumbnail and then to
  `sendDocument`, so an unusual codec still delivers the file.
* **`job.js`** — one job end to end: status throttling, chat actions, upload, cleanup,
  and the error→human translation table.

## Lifecycle of one download

```
User sends a link
   │
   ├─ handler: find the URL, reject anything that is not a single YouTube video
   ├─ handler: quota (active job? cooldown? daily cap?)
   ├─ handler: create the status message ("⏳ Contacting the worker…") + Cancel button
   ├─ handler: insert a download_jobs row
   ├─ handler: POST /download  (Bearer secret, 3 attempts, 12 s each)
   │     ├─ worker: validate payload → 400 with every problem listed
   │     ├─ worker: duplicate chat/user → 409
   │     ├─ worker: queue full → 503
   │     └─ worker: accepted → 202 { job_id, position }   ← answers immediately
   ├─ handler: edit the status message ("Starting now" / "Queued at position 2")
   │
   └─ worker (asynchronously, in its own queue slot):
        ├─ status: 🔎 Resolving streams with TypeTube…      ← edits the same message
        ├─ TypeTube: resolve → downloadMedia (4 range workers) → FFmpeg mux
        │     └─ on any failure: log the real reason, redo it with yt-dlp
        ├─ size check → over 50 MB? retry at the next smaller quality (max 2 tries)
        ├─ thumbnail fetch (mqdefault.jpg, ≤ 200 kB)
        ├─ status: ⬆️ Uploading 12.3 MB…
        ├─ sendVideo (+thumbnail) → sendVideo → sendDocument
        ├─ delete the status message
        └─ remove the job directory
```

## State ownership (and the one real limitation)

* **The worker is the source of truth for live jobs.** It knows what is queued, what is
  running and how far along it is.
* **The bot's `download_jobs` table is a ledger, not a live mirror.** Telegram Serverless
  gives no inbound path for the worker to call back into, so the bot cannot be told "job
  finished".
* Consequences, all handled deliberately:
  * "already running" is decided by the worker's `409` (authoritative) plus a short
    local window that only absorbs double-taps;
  * `/status` asks the worker for the truth instead of trusting the row;
  * stale `queued` rows are expired when they are older than the cooldown.

If you ever need exact end-to-end state, the fix is an intermediate store both sides can
reach (for example a Telegram Mini App endpoint or an external database) — a deliberate
change in scope, not a bug.

## Security model

| Concern | Measure |
|---|---|
| Worker exposure | Only `/` and `/health` are unauthenticated. `/download`, `/jobs/:id/cancel` and `/stats` require the shared bearer secret, compared with `timingSafeEqual` |
| Secret at rest | Stored in the bot's Telegram database, set by `/setup`, which deletes your message. Nothing secret is ever committed (`tests/structure.test.mjs` asserts the config file stays empty) |
| Input | Both sides validate URLs against a host allowlist and require a single-video YouTube link; the worker re-validates everything the handler sends |
| Request size | 16 kB body cap, JSON parse errors handled as 400 |
| Logging | A redacting logger; secrets never appear in logs or in `/health` output |
| Abuse | One job per chat and per user, per-user cooldown, daily quota, bounded queue |
| Blast radius | A compromised worker can upload to your bot's chats. Rotate `WORKER_SECRET` in the Render dashboard and `/setup` again |

## Failure modes

| Failure | Behaviour |
|---|---|
| Worker asleep (Render free) | The submit retries 3× with backoff; the user is told the worker is waking up, and the keep-alive job in the deployment guide prevents it |
| Worker unreachable / wrong secret | Specific copy for each, with the exact fix (`/setup` again) |
| TypeTube 403 (region) | Explains the Singapore cluster relationship and suggests region-restricted music |
| TypeTube fails generally | yt-dlp fallback, with the reason extracted from its stderr |
| File too large | Downgrade ladder, then buttons offering the next smaller qualities |
| User cancels | AbortController stops the download, kills FFmpeg/yt-dlp, deletes the temp dir, edits the status message |
| Deploy during a job | SIGTERM asks jobs to stop, drains up to 30 s, then exits — status messages say "cancelled" rather than lying |
| Worker OOM (512 MB) | Concurrency is 1 by default, chunk size 8 MB, 4 range workers, and the queue refuses work rather than growing without bound |

## Testing strategy

| Layer | How it is tested |
|---|---|
| URL/command rules | The **same fixture file** (`worker/test/fixtures/url_vectors.json`) is run against both the worker's and the bot's implementation, so the two copies cannot drift |
| Worker internals | 62 unit tests: config validation, queue semantics, server routing/auth, yt-dlp parsing, error copy, status throttling, runtime detection |
| Serverless handlers | 69 tests that execute the **real handler modules** with `sdk` resolved to an in-memory stub (`tests/stubs/`) — DB round-trips, HTTP behaviour via a programmable `fetch`, and the exact text users receive |
| Platform rules | `scripts/check_modules.mjs`: identifier filenames, flat `handlers/`, `.js` extensions on relative imports, no Node builtins or npm packages under `tgcloud/`, no foreign keys |
| Container | CI builds `worker/Dockerfile` and smoke-tests `/health` inside it |
