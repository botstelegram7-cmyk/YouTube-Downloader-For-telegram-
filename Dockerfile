# Download worker image, built from the repository root.
#
# Why this file exists next to worker/Dockerfile: some platforms build the repository
# root and look for /Dockerfile, with no way to point at a subdirectory (Voroa's
# "Docker (Dockerfile)" runtime, for example, takes a path but defaults to /Dockerfile).
# This image is the same service as worker/Dockerfile — it just installs the worker from
# a root-level build context.
#
# Node 24 is deliberate: TypeTube needs zlib zstd (Node >= 22.15 / >= 23.8) to read its
# cluster's compressed TAP frames. FFmpeg merges video+audio and normalises audio to M4A;
# yt-dlp is the fallback engine. See docs/TYPETUBE_COMPAT.md.
FROM node:24-bookworm-slim

ENV NODE_ENV=production \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONUNBUFFERED=1 \
    YTDLP_BINARY=yt-dlp

# ffmpeg + ca-certificates for muxing and TLS; python3/pip only to install yt-dlp.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      ffmpeg \
      python3 \
      python3-pip \
 && rm -rf /var/lib/apt/lists/* \
 && pip3 install --break-system-packages --no-cache-dir yt-dlp \
 && npm cache clean --force

WORKDIR /app

# Dependencies first so code changes do not invalidate the npm layer. `npm ci` keeps the
# build reproducible from the committed lockfile.
COPY worker/package.json worker/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund

COPY worker/src ./src

# The platform injects PORT (Voroa always passes 3000); 8080 keeps `docker run` useful
# locally. The worker reads it from the environment either way.
ENV PORT=8080
EXPOSE 8080

# Run unprivileged. The image's node user owns nothing in /app, and the worker only
# writes to the system temp dir.
USER node

# Voroa and Render both use a declared HEALTHCHECK to decide whether a new deploy is
# ready, so this is what makes a bad build fail instead of serving errors.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD node -e "fetch('http://127.0.0.1:'+(process.env.PORT||8080)+'/health').then(r=>process.exit(r.ok?0:1)).catch(()=>process.exit(1))"

CMD ["node", "src/index.js"]
