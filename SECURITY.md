# Security policy

## Reporting a vulnerability

Please **do not** open a public issue for a security problem.

Use GitHub's private channel: repository → **Security** → **Report a vulnerability**
(Private Vulnerability Reporting). If that is unavailable, open a minimal issue that says
only that you have a security report and how to reach you — no details, no proof of concept.

Please include what you can: affected version or commit, what an attacker gains, the steps
to reproduce, and any environment detail that matters (public bot? `OWNER_USER_ID` set?
webhook secret configured?).

Expect an acknowledgement within a few days. This is a hobby-scale project maintained by one
person, with no paid support and no bug bounty — but security reports are taken seriously and
fixed before anything else.

**Never include a live `BOT_TOKEN`, `WEBHOOK_SECRET`, `TELEGRAM_API_HASH` or a cookies file
in a report.** If one leaks, revoke it first (`/revoke` in @BotFather, a new secret in the
dashboard, `my.telegram.org` → terminate sessions), then tell us.

## Supported versions

Only the latest release on `main` is supported. Version 3.x is the Python rewrite; the 2.x
serverless/worker line is unmaintained and should not be deployed.

## What the bot stores

Nothing user-specific and nothing permanent:

* **No database, no message history, no analytics.** Job state lives in memory and is gone
  when the process restarts.
* **Downloaded files** live in the job's own temp directory under `DOWNLOAD_DIR` and are
  removed when the job ends (`CLEANUP_AFTER_UPLOAD`). A `SIGKILL`ed container can leave
  orphaned `job-*` directories behind; deleting them is always safe.
* **Secrets** are read from environment variables or a mounted file and are never written to
  disk, never logged, and never included in error messages. `python main.py --print-config`
  is covered by a test asserting the token cannot appear in its output.
* **Cookies** (`COOKIES_FILE`) are read-only input for yt-dlp and are never copied into a job
  directory or sent anywhere.

## How the bot is hardened

| Area | Protection |
|---|---|
| Access | `OWNER_USER_ID` restricts the bot to its owner. Without it, anyone who knows the bot can use it — set it for a personal deployment. |
| Webhook | `POST /<WEBHOOK_PATH>` requires `X-Telegram-Bot-Api-Secret-Token`, refuses bodies over 2 MB, and queues updates instead of processing them in the request. Without `WEBHOOK_SECRET`, anyone who learns the URL can post fake updates — always set it in production. |
| SSRF | Links are validated before yt-dlp sees them: `http(s)` only, blocked hostnames, and DNS results checked, so a hostname resolving to `127.0.0.1`, a private range or `169.254.169.254` (cloud metadata) is refused. `ALLOW_PRIVATE_URLS=true` disables this and is meant for local testing only. |
| Subprocesses | Every child process is spawned with an argument list (no shell), with a timeout and a registry entry, and is killed on cancel or shutdown. User input never reaches a shell. |
| Container | Runs as non-root (uid 10001) with only `DOWNLOAD_DIR` writable; no port besides `PORT` needs to be published. |
| Rate limiting | Optional `AIORateLimiter` from python-telegram-bot keeps the bot inside Telegram's limits; uploads honour `RetryAfter`. |
| Disk | Jobs are size-estimated before downloading and refused when they exceed the ceiling, so a shared host cannot be filled by a single request from a stranger (that ceiling is low by default: 50 MB on the cloud API). |

## Deployment checklist

- [ ] `BOT_TOKEN` set only as an environment variable or platform secret — never in git.
- [ ] `OWNER_USER_ID` set, unless the bot is deliberately public.
- [ ] `WEBHOOK_SECRET` set (and matching what the bot registers) when using webhook mode.
- [ ] Public HTTPS in front of `PORT`; nothing else exposed.
- [ ] `ALLOW_PRIVATE_URLS` **false**.
- [ ] `COOKIES_FILE`, if used, mounted read-only and out of the repository.
- [ ] `LOG_LEVEL=INFO` (or `WARNING`) in production; `DEBUG` logs more about who sends what.
- [ ] `/health` reachable by your monitor only, or accept that it is a small public status page.
- [ ] Update yt-dlp regularly: extractor fixes are security-relevant, since a malicious page
      is parsed by this process.

## Out of scope

* Downloading content you are not allowed to download. The bot ships no DRM circumvention,
  no key extraction and no paywall bypass; that is by design, not a vulnerability.
* Sites choosing to block datacentre IPs (a proxy is a deployment choice).
* Anything requiring access to the host beyond the container: this project assumes the
  operator controls the host.
* Denial of service through legitimate heavy use on a deliberately public bot without
  `OWNER_USER_ID` — see the checklist above.
