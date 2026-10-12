# Architecture

One process, one container, no external workers. Everything below describes the code in
this repository as it is: the module names are real files, and the numbers are the actual
defaults.

## Contents

- [Why a single service](#why-a-single-service)
- [Module map](#module-map)
- [Lifecycle of one download](#lifecycle-of-one-download)
- [The job model](#the-job-model)
- [Progress reporting](#progress-reporting)
- [Cancellation](#cancellation)
- [Uploads and metadata](#uploads-and-metadata)
- [Security model](#security-model)
- [Failure modes](#failure-modes)
- [Testing strategy](#testing-strategy)

---

## Why a single service

The previous generation of this project was split in two: a serverless Telegram handler and
a separate download worker that talked to each other over HTTP with a shared secret. That
split existed because the serverless platform could not run FFmpeg for long enough.

A Python web service has no such constraint, so both halves were merged. What that buys:

* **No shared secret, no worker endpoint, no HTTP contract to version.** The old design had
  three places where a mismatch could break every download.
* **One place where a job lives.** The previous design had to reconcile "the worker thinks
  this job is running" with "the platform thinks this invocation is still alive".
* **Real cancellation.** Killing yt-dlp/FFmpeg is a local `SIGTERM` to a registered child
  process instead of a request to another service that might itself have died.
* **One artifact to deploy.** `Dockerfile` at the root, `python main.py`, done.

The trade-off is honest and worth stating: a single instance means jobs share one CPU and
one disk, and the service must stay running (a free tier that sleeps adds 30–60 s to the
first message after idle). `MAX_CONCURRENT_JOBS` and the queue bound the first; the
keep-awake ping in the deployment guide softens the second.

---

## Module map

```
main.py ─ CLI and entry point
  ├── --check / --print-config / --version : validate and describe the configuration
  └── run_bot()                            : bot/runner.py
```

| Module | Responsibility | Depends on |
|---|---|---|
| `config.py` | The whole configuration surface: a frozen `Settings` dataclass, `load_settings()`/`SETTINGS`, range validation, derived values (`upload_limit_mb`, `runs_webhook`, `api_mode`) and the startup summary. Never imports the bot. | stdlib only |
| `bot/runner.py` | Process lifecycle: `initialize → on_startup → start → (web server + set_webhook \| polling) → wait for SIGTERM/SIGINT → graceful shutdown`. | app, web |
| `bot/app.py` | Builds the PTB `Application`: handler registration, `concurrent_updates(True)`, HTTP timeouts, `base_url`/`base_file_url` for a local Bot API server, optional rate limiter, the startup introduction and the command menu. | handlers, JobManager |
| `bot/web.py` | The aiohttp app: `/` service card, `/health` + `/healthz` (503 without FFmpeg), `/stats` job snapshot, `POST /<WEBHOOK_PATH>` (secret check, 2 MB payload cap, `Update.de_json`, queue put, immediate 200). | probe, jobs |
| `bot/handlers/` | Thin adapters. `commands.py` (`/start /help /status /cancel /limits /id /about`), `links.py` (URL extraction → probe → keyboard), `callbacks.py` (`dl:`/`cancel:`/`abort:`), `errors.py` (last-resort handler + `RetryAfter`/`Forbidden` handling). | everything below |
| `bot/jobs.py` | `Job` + `JobManager`: one job per user, auto-cancel, `Semaphore` queue, the download→mux→thumbnail→upload pipeline, progress rendering, cleanup, counters. | download, upload, progress |
| `bot/download.py` | yt-dlp: option building, format selection, progress hooks, cancellation through `ProcessRegistry`, `DownloadResult` with metadata from the **local file**. | probe, ffmpeg, process |
| `bot/upload.py` | `sendVideo`/`sendAudio`/`sendDocument`: caption, thumbnail, `supports_streaming`, dimension/duration metadata, byte-accurate upload progress, container-refusal fallback. | progress |
| `bot/ffmpeg.py` | ffprobe wrapper, `+faststart` detection and remux, thumbnail generation, audio conversion, version reporting. | process |
| `bot/probe.py` | Link inspection without downloading: yt-dlp info extraction, ffprobe inspection for direct media URLs, `MediaInfo`, `ProbeCache` (8-char tokens in `callback_data`). | — |
| `bot/qualities.py` | Turns a probed `info` dict into the quality buttons: which heights exist, size estimates (`≈` when derived from bitrate), format selectors. Pure functions — heavily unit-tested. | — |
| `bot/progress.py` | `StatusReporter`: throttled, idempotent status edits with a live cancel button; `UploadProgressFile`: a file wrapper that counts uploaded bytes. | — |
| `bot/process.py` | `ProcessRegistry`: spawns children with pipes, streams output, terminates them (SIGTERM → grace → SIGKILL). The only place that touches subprocesses. | — |
| `bot/urls.py` | URL extraction from message text and the SSRF guard (scheme allow-list, blocked hosts, DNS resolution check). | — |
| `bot/keyboards.py`, `bot/texts.py` | Inline keyboards and every user-facing string (no strings scattered in logic). | — |
| `bot/utils.py`, `bot/errors.py`, `bot/logging_setup.py`, `bot/runtime.py` | Formatting helpers, the error hierarchy with user-facing `reason` text, logging setup, and the per-application `Runtime` that holds settings, jobs, probe cache and tracked background tasks. | — |

Dependency direction is strictly one-way: `config` ← `bot.*` ← `handlers` ← `app` ← `runner`.
`config.py` never imports the bot package, which is what makes `python main.py --check` safe
to run with no token, no network and no FFmpeg.

---

## Lifecycle of one download

```
① update arrives          web.py (webhook) or PTB Updater (polling) → application.update_queue
② link handler            handlers/links.py
                            • urls.extract() — pull the URL out of the message
                            • urls.check_url_safety_async() — SSRF guard (DNS checked too)
                            • reply "🔍 Analysing…" immediately, then work in a tracked task
③ probe                   probe.probe() — yt-dlp in a worker thread (45 s limit)
                            • direct media URL (no geometry) → ffprobe reads the remote file
                            • result → ProbeCache.put() → 8-char token for this user
④ quality keyboard        qualities.build_options() → only heights the source really has,
                            each with a size estimate → keyboards.quality_keyboard()
⑤ button press            handlers/callbacks.py → token → cached info → job_manager.start()
⑥ job                     jobs.py
                            • cancel this user's previous job (processes, temp dir, message)
                            • await semaphore (MAX_CONCURRENT_JOBS)
                            • status message with "❌ Cancel download"
                            • download → mux/faststart → thumbnail → upload
⑦ upload                  upload.py → sendVideo (streaming, thumbnail, real metadata)
⑧ cleanup                 status message deleted, job directory removed, counters updated
```

Steps ③–⑧ never block the update loop: step ② returns an acknowledgement in milliseconds,
and the rest runs in tracked background tasks (`Runtime.track`), so a slow probe cannot
delay other users' commands.

---

## The job model

`JobManager` keeps **one `Job` per user** in a plain `dict[int, Job]`:

```python
class Job:
    id: str  # 8 hex chars, printed in every log line of the job
    user_id: int
    chat_id: int
    status_message: Message
    cancel_event: asyncio.Event
    task: asyncio.Task  # the pipeline
    registry: ProcessRegistry
    workdir: Path  # DOWNLOAD_DIR/job-<id>-*
    counters: {completed, failed, cancelled}
```

Rules that fall out of this design:

* **A new request cancels the old one.** `JobManager.start()` calls `_cancel()` on the
  user's existing job: it sets the cancel event, cancels the task, terminates every child
  process, deletes the work directory and rewrites the old status message to
  "♻️ Cancelled your previous request to start the new one." There is no window in which two
  jobs of one user fight over the same keyboard, token or files.
* **Per-user isolation is structural.** The dict key is the user id; `/status` reports only
  the caller's jobs (plus global counters).
* **Global concurrency is bounded** by `asyncio.Semaphore(MAX_CONCURRENT_JOBS)` (default 2).
  A job that cannot get a slot says so in its status message and starts when one frees up.
* **Everything a job owns lives in its own directory**, so cleanup is a single
  `shutil.rmtree`, and a crashed container can leave only orphaned `job-*` directories that
  are trivially safe to delete.
* **Counters are read-only state for `/status` and `/stats`**: completed, failed, cancelled,
  plus the number of active jobs.

`/cancel` (`JobManager.cancel_user`) is the same `_cancel()` path, so a manual cancel and an
auto-cancel behave identically: processes killed, files deleted, message updated.

---

## Progress reporting

`StatusReporter` owns exactly one Telegram message per job and is the only component that
edits it.

* **Throttled.** Edits are at most one per `PROGRESS_INTERVAL` (default 2 s). Telegram
  rate-limits message edits and does not care that a download moved from 43 % to 44 %.
* **Thread-safe.** yt-dlp runs in a worker thread, so its hooks call
  `update_from_thread()`, which hops back to the event loop with `call_soon_threadsafe`.
* **Idempotent finish.** `finish()` writes the final text once; later calls are no-ops.
  Without this, a cancel racing the last progress update could overwrite the outcome.
* **Self-healing.** A failed edit (Telegram hiccup, "message is not modified", a deleted
  message) is logged at debug level and never propagates into the job.
* **Actionable.** Every edit re-attaches the "❌ Cancel download" button, so the control
  stays available for the whole download→upload window. `finish()` removes it.

Upload progress is measured from the bytes actually read from disk: `UploadProgressFile`
wraps the file handle and counts `read`/`readinto` calls, which is what the HTTP client
really consumes — no guessing from a percentage the API does not report. `reset()` rewinds
to zero, which is what the `sendVideo`→`sendDocument` fallback needs.

---

## Cancellation

Three layers, in order of how fast they bite:

1. **Cooperative.** `job.cancel_event` is checked between phases and inside progress
   callbacks, so a cancel is observed at the next safe point even before any signal.
2. **Asyncio.** The job task is cancelled, ending awaits (including `wait_for` wrappers
   around the download, which run in a thread).
3. **Process.** `ProcessRegistry.terminate_all()` sends `SIGTERM` to every live child
   (yt-dlp, FFmpeg, ffprobe), waits a grace period, then `SIGKILL`s survivors.

Because the download runs through the same registry, `/cancel` stops a download that is
mid-write, not just a job that happens to be between phases. On shutdown,
`on_shutdown()` calls `cancel_all()`, so a container stop does not leave stray FFmpeg
processes or half-written files behind.

---

## Uploads and metadata

The delivery path exists to make the result *playable*, not merely present:

| Concern | What happens |
|---|---|
| Metadata | `duration`, `width`, `height` come from ffprobe on the **downloaded file**, not from the source's claims — a direct link with no metadata still arrives with correct geometry. |
| Streaming | MP4 is remuxed with `+faststart` when needed, and `supports_streaming=True` is set. |
| Thumbnail | FFmpeg grabs a frame ~20 % in, scales it to ≤ `THUMBNAIL_MAX_SIDE` (320) and encodes JPEG under Telegram's 200 kB thumbnail limit, then attaches it. |
| Caption | Title, quality, resolution, duration, size and the source link — capped at 1000 characters, HTML-escaped. |
| Audio | `sendAudio` with title/performer tags; `m4a` needs no re-encode, `mp3` is converted by FFmpeg. |
| Documents | Only when Telegram refuses the container: the same bytes go out as a document and the reason is logged. |
| Retry | A failed job offers a "↻ Try a smaller quality" keyboard built from the cached probe, so the user does not have to re-send the link. |

---

## Security model

**Who can use the bot.** With `OWNER_USER_ID` set, any other user is ignored — the bot is
not a public service in that mode. Without it, anyone who finds the bot can use it; think
about that before deploying a public one.

**Webhook endpoint.** `POST /<WEBHOOK_PATH>` requires the
`X-Telegram-Bot-Api-Secret-Token` header to equal `WEBHOOK_SECRET` (403 otherwise) and
refuses bodies over 2 MB (413). Responses are always immediate: the update is queued, not
processed inside the request.

**SSRF.** URLs are validated before they reach yt-dlp: only `http(s)`, no blocked hostnames,
and the **resolved** addresses are checked, so a hostname that resolves to `169.254.169.254`
(cloud metadata), `127.0.0.1` or a private range is rejected. `ALLOW_PRIVATE_URLS=true`
disables that guard for local testing — it is a deliberate hole.

**Subprocesses.** Every child process is spawned with an explicit argument list (never a
shell), with a timeout and a registry entry, and is killed on cancel or shutdown. No
user-supplied string reaches a shell.

**Secrets.** `BOT_TOKEN`, `WEBHOOK_SECRET`, `TELEGRAM_API_HASH` and cookie files are read
from the environment or a mounted file and never logged: `Settings.summary()` prints a
token *presence* flag instead of the value, `main.py --print-config` is covered by a test
that asserts the token cannot leak, and error messages pass Telegram's text through rather
than paraphrasing it.

**What is stored.** Nothing per user beyond in-memory job state. Files live in the job's
temp directory and are removed when the job ends. There is no database, no analytics and no
message history.

---

## Failure modes

| Failure | Behaviour |
|---|---|
| Bad link / unsupported site | Friendly message with the reason and the three usual causes; no job is created. |
| Slow or hanging probe | 45 s limit (`INSPECT_TIMEOUT`), then "could not read that link" with the reason. |
| Download error | yt-dlp's message is passed through and cleaned; the job ends, temp files are removed, the retry keyboard offers smaller qualities. |
| Download hangs | `DOWNLOAD_TIMEOUT_SECONDS` (default 3600) ends it. |
| Too large | Refused **before** downloading, from the size estimate, with the real ceiling named. |
| Telegram rate-limits the upload | `RetryAfter` is honoured and reported in the status message. |
| Telegram refuses the container | Same bytes go out as a document; logged. |
| The user blocked the bot | Job ends quietly; logged at info level. |
| Telegram edit fails | Logged at debug level; the job continues. |
| Container SIGTERM | `cancel_all()` kills processes and cleans up, then "Stopped. Goodbye." |
| Unhandled exception in a handler | `handlers/errors.py` logs the traceback with the job/update id and tells the user instead of going silent. |
| FFmpeg missing | `/health` returns 503 and `main.py --check` fails loudly, so deploys catch it. |

---

## Testing strategy

Three layers, each catching what the one below cannot:

1. **Pure unit tests** — `config.py`, `qualities.py`, `urls.py`, `progress.py` (70 tests
   overall). Quality selection, size estimation, SSRF rules, throttling, and the
   no-token-leak guarantee are all asserted directly.
2. **Integration tests with real media** — `tests/test_ffmpeg.py` builds genuine H.264/AAC
   clips and exercises probing, faststart remuxing, thumbnail limits and audio conversion.
   They skip themselves when no FFmpeg is installed, so the suite stays green on a bare
   machine.
3. **End-to-end harness** — `scripts/e2e_local.py` runs the real bot in webhook mode against
   a mock Bot API, sends it a link to a locally served clip, presses a quality button and
   asserts the entire delivery: startup introduction, command menu, quality buttons,
   `sendVideo` with caption + thumbnail + `supports_streaming` + dimensions + duration,
   status-message cleanup, temp-directory cleanup, and a clean SIGTERM shutdown. Nothing is
   mocked except Telegram itself.

CI runs all three on every push — plus a Docker job that rebuilds the image and runs the
e2e *inside* it, which is what proves the container has FFmpeg, the right permissions and a
working `--check`. It also runs a hygiene job that fails on accidentally committed secrets,
serverless leftovers or stray artifacts.
