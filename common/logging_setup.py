"""Rotating, structured logging used by both the agent and the client."""

from __future__ import annotations

import logging
import logging.handlers
import os
import time
from pathlib import Path

LOG_FORMAT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

_configured: set[str] = set()


def default_state_dir(app: str = "rodm") -> Path:
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return Path(base) / app


def setup_logging(
    name: str,
    *,
    log_dir: Path | str | None = None,
    level: int = logging.INFO,
    console: bool = True,
    log_file: str | None = None,
) -> logging.Logger:
    """Configure a rotating file handler (plus console) exactly once per name."""
    logger = logging.getLogger(name)
    if name in _configured:
        return logger
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(LOG_FORMAT, datefmt=DATE_FORMAT)

    if console:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        logger.addHandler(stream)

    if log_dir is not None:
        directory = Path(log_dir)
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / (log_file or f"{name.replace('.', '_')}.log")
        file_handler = logging.handlers.RotatingFileHandler(
            target, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    _configured.add(name)
    return logger


class LogThrottler:
    """Avoid flooding the log when a fast operation repeats messages."""

    def __init__(self, logger: logging.Logger, interval: float = 2.0) -> None:
        self.logger = logger
        self.interval = interval
        self._last: dict[str, float] = {}

    def info(self, key: str, message: str) -> None:
        now = time.monotonic()
        if now - self._last.get(key, 0.0) >= self.interval:
            self._last[key] = now
            self.logger.info(message)


def format_bytes(count: float) -> str:
    units = ["B", "KiB", "MiB", "GiB", "TiB"]
    value = float(count)
    for unit in units:
        if abs(value) < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(value)} {unit}"
            return f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"  # pragma: no cover
