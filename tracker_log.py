from __future__ import annotations

import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path


LOG_DIR = Path(__file__).resolve().parent / "logs"
MAX_BYTES = 2 * 1024 * 1024
BACKUP_COUNT = 5

_REDACTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"([?&](?:token|key)=)[^&\s\"']+", re.IGNORECASE), r"\1[REDACTED]"),
    (
        re.compile(
            r'("(?:token|password|apiKey|api_key|key|secret|authorization)"\s*:\s*")[^"]+(")',
            re.IGNORECASE,
        ),
        r"\1[REDACTED]\2",
    ),
    (re.compile(r"\b(Basic|Bearer)\s+[A-Za-z0-9+/=_\-.]{8,}"), r"\1 [REDACTED]"),
    (
        re.compile(
            r"\b(password|token|secret|api_key|apikey|authorization)\s*[=:]\s*\S+",
            re.IGNORECASE,
        ),
        r"\1=[REDACTED]",
    ),
    (re.compile(r"\b([0-9a-fA-F]{8})[0-9a-fA-F\-]{24,}\b"), r"\1…[REDACTED]"),
    (re.compile(r"\b(\d{6})\d{11,}\b"), r"\1…[REDACTED]"),
]


def redact(value: str) -> str:
    for pattern, replacement in _REDACTIONS:
        value = pattern.sub(replacement, value)
    return value


class _UtcFormatter(logging.Formatter):
    converter = time.gmtime

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def get_logger(component: str, filename: str | None = None) -> logging.Logger:
    logger = logging.getLogger(f"valorant_tracker_core.{component}")
    if logger.handlers:
        return logger
    logger.setLevel(logging.INFO)
    logger.propagate = False
    try:
        LOG_DIR.mkdir(exist_ok=True)
        handler = RotatingFileHandler(
            LOG_DIR / f"{filename or component}.log",
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        handler.setFormatter(
            _UtcFormatter(
                fmt=f"%(asctime)s.%(msecs)03dZ [{component}] %(levelname)s %(message)s",
                datefmt="%Y-%m-%dT%H:%M:%S",
            )
        )
        logger.addHandler(handler)
    except OSError:
        logger.addHandler(logging.NullHandler())
    return logger


def log_code(logger: logging.Logger, level: int, code: str, message: str) -> None:
    logger.log(level, "%s %s", code, message)
