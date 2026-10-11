# TypeTube on this worker: the Node version story

TypeTube is the primary download engine here. It has one hard runtime requirement that
is easy to miss, and this document explains exactly why, what happens when it is not
met, and what the worker does about it.

## The short version

| Node version | TypeTube | Why |
|---|---|---|
| 18, 20 | ✗ crash on import | `zlib.zstdCompress` does not exist, and `typetube` calls `promisify()` on it at module load |
| 22.0 – 22.14 | ✗ crash on import | zstd was backported in 22.15.0 |
| **22.15+, 23.8+, 24, 25, 26** | ✓ works | `zlib zstd` is available |

The worker image therefore pins **Node 24** (`worker/Dockerfile`), and
`worker/package.json` declares `"engines": { "node": ">=22.15.0" }`.

## The bug, precisely

`typetube@1.1.4` ships `dist/tap/compression.js` starting with:

```js
const zstdCompressAsync = promisify(zlib.zstdCompress);   // line 6 — module scope
```

zstd support landed in Node **v23.8.0** and was backported to **v22.15.0**. On any
older runtime `zlib.zstdCompress` is `undefined`, so `promisify(undefined)` throws
while the module is being *imported*:

```
TypeError [ERR_INVALID_ARG_TYPE]: The "original" argument must be of type function.
Received undefined
    at promisify (node:internal/util:409:3)
    at .../typetube/dist/tap/compression.js:6:27
```

Nothing in the importing program has run yet, and the stack trace points at
`node:util`, not at TypeTube — which is why this wastes so much time.

The correct upstream fix is to resolve the function lazily:

```js
const zstdCompressAsync =
  typeof zlib.zstdCompress === 'function' ? promisify(zlib.zstdCompress) : null;
```

and to throw a clear error only when compression is actually needed. That fix is
prepared as a separate pull request against the TypeTube repository
(`fix/lazy-zstd-compression`).

## Why a polyfill is not enough

It is tempting to shim `zlib.zstdCompress` with brotli before importing. The worker
does install exactly such a shim — but only to convert the confusing crash into a
readable error. It cannot make TypeTube *work* on old Node:

* TAP frames are negotiated with an `ACCEPT_COMPRESSED` flag, and the cluster answers
  with **real zstd frames**;
* Node 20 has no zstd decompressor at all, so those frames cannot be read.

Measured on Node 20.20.2 with the shim installed: `import 'typetube'` succeeds,
`createClient()` succeeds, and then `resolve()` fails with
`Error: Decompression failed`. There is no way around it short of bundling a zstd
decoder — which is exactly what upgrading Node gives you for free.

## What the worker does

1. **At boot** `worker/src/typetube.js` checks the runtime. A missing zstd is reported
   in `/health` as `typeTube: { supported: false }` and logged as a warning. The
   process still starts, so the platform health check passes and you can see the reason
   instead of a crash loop.
2. **On every job**, `loadTypeTube()` refuses to import TypeTube on an unsupported
   runtime and raises `UNSUPPORTED_RUNTIME` with instructions. If `YTDLP_FALLBACK=true`
   (the default) the job proceeds with yt-dlp, so users still get their file.
3. If you would rather fail loudly, set `STRICT_STARTUP=true` — the worker then refuses
   to boot on an unsupported runtime.

Health endpoint while running on old Node:

```json
{
  "typeTube": {
    "supported": false,
    "zstd": false,
    "note": "TypeTube disabled: needs Node >= 22.15"
  }
}
```

## Checking your own runtime

```bash
node -e "const z=require('node:zlib');console.log(process.version, typeof z.zstdCompress)"
# v24.9.0 function     ← good
# v20.20.2 undefined   ← TypeTube cannot work here
```

Locally (outside Docker) always run the tests with a supported Node:

```bash
node --version && npm --prefix worker test
```

## Related notes

* `zstd` output in Node is still marked experimental (stability 1), which is why some
  packages — Express's `compression` among them — do not negotiate it yet. TypeTube
  falls back to brotli for *its own* payloads, but it cannot decode incoming zstd.
* `native/quickack.node` inside the TypeTube package is a small x86-64 addon that only
  sets `TCP_QUICKACK` on a socket descriptor, and every call site is wrapped in
  try/catch, so a missing or incompatible addon is harmless.
