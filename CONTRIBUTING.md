# Contributing

Thanks for taking a look. This is a small, single-purpose project: a Telegram bot that
downloads media with the TypeTube CLI and delivers it playably. Keeping it small is a feature.

## Contents

- [Set up a dev environment](#set-up-a-dev-environment)
- [Before you push](#before-you-push)
- [Project conventions](#project-conventions)
- [Where things go](#where-things-go)
- [Adding a feature safely](#adding-a-feature-safely)
- [Reporting bugs](#reporting-bugs)

---

## Set up a dev environment

```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-

python3 -m venv .venv
source .venv/bin/activate            # Windows: .venv\Scripts\activate
pip install -r requirements-dev.txt  # runtime deps + pytest, ruff, pytest-asyncio

python main.py --check               # tells you if FFmpeg is found and what is configured
pytest                               # 70 tests, a few seconds
```

You do **not** need a Telegram token to run the tests: nothing talks to Telegram. You also
do not need FFmpeg to run *most* of them — the seven media tests skip themselves without it.
To exercise those:

```bash
# Linux, static build (no root needed)
curl -L -o ffmpeg.tar.xz https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-amd64-static.tar.xz
tar xf ffmpeg.tar.xz
export FFMPEG_BINARY="$PWD/ffmpeg-*-static/ffmpeg"
export FFPROBE_BINARY="$PWD/ffmpeg-*-static/ffprobe"
pytest -q
```

For the full pipeline (the real bot, a mock Bot API and a real clip):

```bash
python scripts/e2e_local.py                 # needs ffmpeg/ffprobe on PATH
python scripts/e2e_local.py --ffmpeg /opt/ffmpeg --keep-files
```

It is the fastest way to see a change work end to end — and the fastest way to see *why* it
does not, because it prints the bot log on failure.

## Before you push

```bash
ruff check . && ruff format --check .   # must be clean; ruff format . to fix
pytest                                  # all green (or green-with-skips)
python main.py --check                  # config sanity with your environment
python scripts/e2e_local.py             # if you touched the pipeline
```

CI runs exactly these (plus a Docker build and a hygiene check), so running them locally
saves a round trip.

## Project conventions

**Style.** Python 3.10+, type hints on public functions, `from __future__ import annotations`
at the top, line length 120 (ruff's rules live in `pyproject.toml`). F-strings, `pathlib`,
`logging` with a module-level `log = logging.getLogger("bot.something")`.

**No blocking calls in async code.** Anything slow (the TypeTube CLI, FFmpeg, DNS, disk walks) goes
through `asyncio.to_thread` or a subprocess registered in `ProcessRegistry`. This is the
rule most likely to be broken by a well-meaning patch, and it freezes the bot for *everyone*
when it is.

**Text lives in `bot/texts.py`.** All user-facing strings, one place, HTML-escaped where
needed. New text goes there, not inline in handlers.

**Errors.** Raise from the hierarchy in `bot/errors.py`; each class carries a user-facing
`reason`. Handlers translate errors into messages with `bot/handlers/errors.py` as the last
resort. Never let a bare `Exception` reach the user as a traceback.

**Settings.** Anything configurable is a field on `Settings` in `config.py` with a default, a
range check and an entry in `.env.example`, `README.md` and `docs/DEPLOYMENT.md`. No magic
numbers buried in logic.

**Comments** explain *why*. The code already says what it does.

## Where things go

| Change | Files |
|---|---|
| A new command | `bot/handlers/commands.py`, the `COMMANDS` tuple in `bot/texts.py`, `add_handler` in `bot/app.py` |
| A new button | `bot/keyboards.py` (callback data ≤64 bytes), a branch in `bot/handlers/callbacks.py` |
| Quality/format behaviour | `bot/qualities.py` (pure functions — add unit tests in `tests/test_qualities.py`) |
| Download behaviour | `bot/download.py`; progress hooks call `StatusReporter.update_from_thread` |
| Upload behaviour | `bot/upload.py`; keep the document fallback and the caption cap |
| FFmpeg work | `bot/ffmpeg.py`; add a real-media test in `tests/test_ffmpeg.py` |
| A new setting | `config.py`, `.env.example`, `README.md` configuration tables |
| A new endpoint | `bot/web.py` (and mention it in `README.md` and `docs/ARCHITECTURE.md`) |

## Adding a feature safely

1. **Say what breaks if it fails.** If the answer is "every job for every user", it belongs
   behind a setting and a try/except with a log line.
2. **Keep the job contract.** One job per user, one status message per job, one temp
   directory per job, cleanup on every exit path (success, failure, cancel, SIGTERM). New
   code that creates a process must register it in `ProcessRegistry`, or `/cancel` will
   silently stop working for that phase.
3. **Test the behaviour, not the mocks.** `tests/test_jobs.py` drives the real
   `JobManager` with a fake Bot object; `tests/test_ffmpeg.py` builds real media. Prefer that
   over mocking the thing under test.
4. **Update the docs in the same commit.** `README.md` for users, `docs/DEPLOYMENT.md` for
   operators, `docs/ARCHITECTURE.md` for the next contributor, `CHANGELOG.md` for everyone
   (`### Added` / `### Changed` / `### Fixed` / `### Removed`).
5. **Keep it honest** (this one is a project rule, not a style preference): never claim a
   capability in the UI or the docs that the code does not actually have — file sizes and
   quality labels especially.

## Reporting bugs

Open an issue with:

* what you did and what happened (the chat text and the bot's reply),
* the log excerpt, redact the token — every job line carries its job id, so
  `grep <job id>` gives you the full story,
* `python main.py --check` output (it includes versions and the effective limits),
* for download failures: the link, or at least the site, and whether the same link works
  with a recent TypeTube CLI (Node 22.15+) on the command line.

For security issues, please do not open a public issue — see [SECURITY.md](SECURITY.md).
