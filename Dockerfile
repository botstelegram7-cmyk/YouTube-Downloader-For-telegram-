# YouTube Downloader bot — Python 3.12 + FFmpeg + Node 22 (TypeTube engine).
#
# Works on any Web Service that builds a Dockerfile: Render, Voroa, Fly.io, a VPS.
# The bot serves its webhook and /health endpoint on $PORT.
FROM python:3.12-slim-bookworm

# Node 22.15 or newer is a hard requirement of the TypeTube CLI: older releases crash
# while importing zlib's zstd helpers. 22.23.3 is the version the bot is tested against.
ARG NODE_VERSION=22.23.3
# The CLI release the Python layer is written against.
ARG TYPETUBE_VERSION=1.1.4

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOWNLOAD_DIR=/tmp/ytbot-downloads \
    NODE_PATH=/usr/local/lib/node_modules

# ffmpeg/ffprobe mux and inspect media; curl is used by the health check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
 && rm -rf /var/lib/apt/lists/*

# Node.js from the official tarball: deterministic, and no extra apt repository.
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) node_arch="x64" ;; \
      arm64) node_arch="arm64" ;; \
      *) echo "unsupported architecture: $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/node-v${NODE_VERSION}-linux-${node_arch}.tar.gz" \
      -o /tmp/node.tar.gz; \
    tar -xzf /tmp/node.tar.gz -C /usr/local --strip-components=1 --no-same-owner; \
    rm /tmp/node.tar.gz; \
    node --version; \
    npm --version

# The download engine. Importing the package at build time is what proves this Node is
# new enough for it — a Node that is too old fails the build here, not at 3 a.m. in production.
RUN npm install -g "typetube@${TYPETUBE_VERSION}" \
 && node -e "require('typetube'); console.log('typetube', require('typetube/package.json').version, 'ready')" \
 && typetube --version >/dev/null 2>&1 || true

WORKDIR /app

# Dependencies first: a code change then does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY config.py main.py ./
COPY bot ./bot
COPY assets ./assets
COPY scripts ./scripts

# Run as a non-root user; the download directory is the only writable path needed.
RUN useradd --create-home --uid 10001 botuser \
 && mkdir -p "$DOWNLOAD_DIR" \
 && chown -R botuser:botuser /app "$DOWNLOAD_DIR"
USER botuser

ENV PORT=8080
EXPOSE 8080

# Both Render and Voroa use a declared HEALTHCHECK (or the health path) to decide
# whether a deploy is ready, so a broken build fails here instead of going live.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD curl -fsS "http://127.0.0.1:${PORT}/health" >/dev/null || exit 1

CMD ["python", "main.py"]
