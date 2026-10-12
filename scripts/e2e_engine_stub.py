#!/usr/bin/env python3
"""A stand-in for the TypeTube CLI, used only by ``scripts/e2e_local.py``.

It answers the two commands the bot issues, with the same shapes the real CLI produces:

* ``resolve <url> --json``      → the resolved track as JSON,
* ``download <url> <dir> …``    → copies the fixture into ``<dir>`` and reports it with a
  ``Completed: <path> (<mb> MB in <s>s)`` line, preceded by ``\\r``-separated progress.

Why a stub: the end-to-end check must prove the bot's *own* pipeline (FFmpeg muxing, faststart,
ffprobe verification, thumbnail, upload, webhook) without depending on YouTube or the network.
Everything downstream of this file is the real thing.

Configured through the environment by the harness:

    E2E_STUB_SOURCE   path of the media file to hand back (required)
    E2E_STUB_TITLE    title reported by ``resolve`` and used for the file name
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

DEFAULT_TITLE = "E2E Test clip"
STUB_VERSION = "1.1.4 (e2e stub)"


def track(source: Path, title: str) -> dict:
    """The ``resolve --json`` payload: two video heights and one audio track."""
    return {
        "id": "jNQXAC9IVRw",
        "title": title,
        "author": "e2e",
        "durationSeconds": 3,
        "thumbnail": "",
        "videoStreams": [
            {
                "itag": 18,
                "quality": "360p",
                "resolution": "640x360",
                "url": "stub://video/360",
                "mimeType": 'video/mp4; codecs="avc1.42001E, mp4a.40.2"',
            },
            {
                "itag": 133,
                "quality": "240p",
                "resolution": "426x240",
                "url": "stub://video/240",
                "mimeType": 'video/mp4; codecs="avc1.4d400b"',
            },
        ],
        "audioStreams": [
            {
                "itag": 140,
                "quality": "130kbps AAC",
                "bitrate": 130171,
                "url": "stub://audio/140",
                "mimeType": 'audio/mp4; codecs="mp4a.40.2"',
            }
        ],
        "meta": {"tLatencyMS": 12, "source": str(source)},
    }


def download(rest: list[str], source: Path, title: str) -> int:
    """Copy the fixture where the CLI would have put the finished file."""
    if len(rest) < 2:
        print("[Error] download needs a url and an output directory", file=sys.stderr)
        return 1

    destination = Path(rest[1])
    destination.mkdir(parents=True, exist_ok=True)
    audio_only = "-x" in rest or "--audio-only" in rest
    target = destination / f"{title.replace('/', '-')}{'.m4a' if audio_only else '.mp4'}"

    if not source.is_file():
        print(f"[Error] E2E_STUB_SOURCE does not exist: {source}", file=sys.stderr)
        return 1

    started = time.time()
    shutil.copyfile(source, target)
    megabytes = target.stat().st_size / 1e6

    # The real CLI writes progress with carriage returns, then a Completed line.
    for percent in (12, 48, 76, 100):
        print(f"[download] [downloading] {percent}% of ~{megabytes:.1f}MB at 9.9MB/s", end="\r", flush=True)
    print(f"[download] [{'extracting audio' if audio_only else 'muxing'}] {target.name}", flush=True)
    print(f"Completed: {target} ({megabytes:.1f} MB in {time.time() - started:.2f}s)", flush=True)
    return 0


def main(argv: list[str]) -> int:
    if not argv:
        return 0

    command, rest = argv[0], argv[1:]
    if command in ("--version", "-V", "version"):
        print(STUB_VERSION)
        return 0

    source = Path(os.getenv("E2E_STUB_SOURCE", ""))
    title = os.getenv("E2E_STUB_TITLE", DEFAULT_TITLE)

    if command in ("resolve", "search"):
        print(json.dumps(track(source, title)))
        return 0
    if command == "download":
        return download(rest, source, title)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
