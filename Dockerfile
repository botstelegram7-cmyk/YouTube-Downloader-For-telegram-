# YouTube Downloader bot — Python 3.12 + FFmpeg + yt-dlp.
#
# Works on any Web Service that builds a Dockerfile: Render, Voroa, Fly.io, a VPS.
# The bot serves its webhook and /health endpoint on $PORT.
FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DOWNLOAD_DIR=/tmp/ytbot-downloads

# ffmpeg/ffprobe mux and inspect media; curl is used by the container health check.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates \
      curl \
      ffmpeg \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first: a code change then does not reinstall them.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# yt-dlp comes from requirements.txt, which installs both the Python package (used
# in-process) and the `yt-dlp` console script, so operators can always debug from a
# shell. Fail the build here if the CLI is missing, rather than at run time.
RUN yt-dlp --version

COPY config.py main.py ./
COPY bot ./bot

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
