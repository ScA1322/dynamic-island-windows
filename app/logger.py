"""Logging setup with a filter that scrubs tokens and secrets from every record."""
from __future__ import annotations

import logging
import re
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_PATTERNS = [
    # JSON / form fields: "access_token": "...", refresh_token=...
    (re.compile(r'((?:access|refresh|id)_token|code_verifier|client_secret|code)(["\']?\s*[:=]\s*["\']?)([^"\'&\s,}]+)', re.I),
     r"\1\2***"),
    (re.compile(r"(Bearer\s+)[A-Za-z0-9._\-~+/=]+", re.I), r"\1***"),
    (re.compile(r"(Authorization[\"']?\s*[:=]\s*[\"']?)[^\"'\n,}]+", re.I), r"\1***"),
]


def redact(text: str) -> str:
    for pattern, repl in _PATTERNS:
        text = pattern.sub(repl, text)
    return text


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            return True
        cleaned = redact(message)
        if cleaned != message:
            record.msg, record.args = cleaned, None
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup_logging(log_dir: Path, level: int = logging.INFO, console: bool = True) -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_file = log_dir / "dynamic_island.log"
    root = logging.getLogger()
    root.setLevel(level)
    for handler in list(root.handlers):
        root.removeHandler(handler)

    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    redactor = RedactingFilter()

    file_handler = RotatingFileHandler(log_file, maxBytes=1_000_000, backupCount=3, encoding="utf-8")
    file_handler.setFormatter(fmt)
    file_handler.addFilter(redactor)
    root.addHandler(file_handler)

    # pythonw / frozen windowed builds have no console
    if console and sys.stderr is not None:
        stream = logging.StreamHandler(sys.stderr)
        stream.setFormatter(fmt)
        stream.addFilter(redactor)
        root.addHandler(stream)

    logging.getLogger("urllib3").setLevel(logging.WARNING)
    return log_file
