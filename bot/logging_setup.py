"""Logging setup: readable console output, quiet third-party loggers."""

from __future__ import annotations

import logging
import sys

_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}
_NOISY = ("httpx", "httpcore", "telegram.ext.Application", "telegram.request", "asyncio")


def setup_logging(level: str = "INFO", *, pretty: bool = True) -> logging.Logger:
    """Configure the root logger once and return this application's logger."""
    severity = level.upper() if level.upper() in _LEVELS else "INFO"

    if pretty:
        fmt = "%(asctime)s | %(levelname)-7s | %(name)-18s | %(message)s"
    else:
        fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(fmt, datefmt="%H:%M:%S"))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(severity)

    for name in _NOISY:
        logging.getLogger(name).setLevel(max(logging.WARNING, logging.getLogger().level))

    return logging.getLogger("bot")


def silence_library(name: str, level: int = logging.WARNING) -> None:
    logging.getLogger(name).setLevel(level)
