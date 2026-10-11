# Contributing

Thanks for taking the time. This repository has two runtimes with very different rules,
so most of the guidance below is about keeping them apart cleanly.

## Setup

```bash
git clone https://github.com/botstelegram7-cmyk/YouTube-Downloader-For-telegram-.git
cd YouTube-Downloader-For-telegram-
npm install
npm run check        # modules + syntax + both test suites
```

Node **22.15+** (24 recommended) is required — the worker's TypeTube dependency needs
zlib zstd. Locally you also want `ffmpeg` if you plan to run the worker end to end.

## Layout rules that are enforced by tests

* Everything the platform deploys lives under **`tgcloud/`**.
* Module filenames must be identifier-like: `my_module.js`, never `my-module.js` or
  `config.example.js`.
* `handlers/` and `endpoints/` are **flat** — one level, no subdirectories.
* Relative imports **must** end in `.js`: `import { x } from '../lib/youtube.js'`.
* Code under `tgcloud/` may import **only** `sdk`, `sdk/*` and relative modules. No
  `node:*` builtins, no npm packages, no `require()`.
* No foreign keys in `schema.js` — they throw when declared. Columns are only dropped
  with `.deprecated('reason')`.
* A handler is `export default async function (input, ctx)`: for `handlers/message`,
  `input` is the **Message** (not the Update).

`npm run check:modules` checks all of this, and `tests/structure.test.mjs` asserts it
again so a bad layout cannot be merged.

## Writing code

* **Keep the two URL validators in sync.** `worker/src/validate.js` and
  `tgcloud/lib/youtube.js` implement the same rules for two runtimes;
  `worker/test/fixtures/url_vectors.json` is executed against both. Add a case to the
  fixture file, not to one implementation only.
* **Keep logic pure where it will be tested.** Handler logic belongs in
  `tgcloud/lib/*` modules that avoid `sdk` imports when possible; I/O glue stays thin.
* **Every user-facing sentence goes in `tgcloud/lib/messages.js`.** No literals in
  handlers.
* **The worker must stay honest about failure.** New error paths need copy in
  `worker/src/job.js` `explainError()` so the user learns what to do next.
* **No secrets in git.** Worker credentials are set with `/setup` and stored in the
  bot's database. `tests/structure.test.mjs` fails if `lib/config.js` gains a credential.
* Match the existing style: 2-space indent, single quotes, full sentences in comments
  that explain *why*, not *what*.

## Tests

```bash
npm test             # Serverless side (handlers run against a stub SDK)
npm run test:worker  # worker unit tests
npm run check        # everything CI runs
```

When you add a feature, add the test that would fail without it. When you fix a bug, add
the test that reproduces it — the suite exists to catch exactly that class of mistake.

## Pull requests

1. Branch from `main`: `fix/…`, `feat/…`, `docs/…`.
2. Keep commits focused; conventional prefixes (`fix:`, `feat:`, `docs:`, `test:`,
   `chore:`) are appreciated.
3. `npm run check` must pass.
4. Describe the **user-visible** change and how you verified it. Screenshots of the
   Telegram conversation are ideal for handler changes.
5. If you changed anything under `tgcloud/`, state that you ran `npx tgcloud push` and
   `npx tgcloud migrate` (or explain why you could not).

## Reporting bugs

Include:

* the exact text the bot replied with (screenshots help);
* `curl -s $WORKER_URL/health` output for worker problems — it carries the Node version,
  FFmpeg path and TypeTube capability;
* the relevant Render log lines (they are one JSON object per line, and secrets are
  redacted);
* what you expected to happen instead.

## Security

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).
