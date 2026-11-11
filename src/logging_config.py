"""
Structured JSON logging configuration for Ghost-Data Suite.

Provides a JsonFormatter that emits one JSON object per log line, suitable for
ingestion by Loki, ELK, CloudWatch, or any JSON-log aggregator.

Usage in any module:

    from logging_config import setup_json_logging
    setup_json_logging(name="ghost-server", log_file="logs/app.log", level="INFO")
    log = logging.getLogger("ghost-server")
    log.info("scrape completed", url="https://example.com", duration_ms=1234)
"""

from __future__ import annotations

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path


class JsonFormatter(logging.Formatter):
    """Formatter that emits structured JSON log records."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "module": record.module,
            "function": record.funcName,
            "line": record.lineno,
        }

        # Add extra structured fields passed via log.info(msg, key=value)
        for key, value in record.__dict__.items():
            if key not in (
                "name",
                "msg",
                "args",
                "levelname",
                "levelno",
                "pathname",
                "filename",
                "module",
                "lineno",
                "funcName",
                "created",
                "msecs",
                "relativeCreated",
                "thread",
                "threadName",
                "process",
                "processName",
                "getMessage",
                "exc_info",
                "exc_text",
                "stack_info",
                "created",
            ):
                log_entry[key] = value

        if record.exc_info:
            log_entry["exception"] = self.formatException(record.exc_info)

        return json.dumps(log_entry, ensure_ascii=False, separators=(",", ":"))


def setup_json_logging(
    name: str = "ghost",
    log_file: str | None = None,
    level: str = "INFO",
    json_logs: bool = True,
) -> logging.Logger:
    """
    Configure structured logging for a module.

    Args:
        name: Logger name (typically __name__).
        log_file: Optional path to a log file. Parent directories are created.
        level: Logging level name (INFO, DEBUG, WARNING, ERROR).
        json_logs: When True, emit JSON to both stdout and file. When False,
                   use plain text (useful for local development).

    Returns:
        Configured logging.Logger instance.
    """
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))
    logger.handlers.clear()  # avoid duplicate handlers on re-import

    # Console handler
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(getattr(logging, level.upper(), logging.INFO))
    if json_logs:
        console.setFormatter(JsonFormatter())
    else:
        console.setFormatter(
            logging.Formatter("%(asctime)s | %(levelname)s | %(name)s | %(message)s")
        )
    logger.addHandler(console)

    # File handler
    if log_file:
        log_path = Path(log_file)
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError:
            log_path = None
        if log_path is not None:
            file_handler = logging.FileHandler(log_path, encoding="utf-8")
            file_handler.setLevel(logging.DEBUG)
            if json_logs:
                file_handler.setFormatter(JsonFormatter())
            else:
                file_handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s | %(levelname)s | %(name)s | %(message)s | %(filename)s:%(lineno)d"
                    )
                )
            logger.addHandler(file_handler)

    return logger
