"""Logging configuration — structlog with JSON renderer + rotating files.

Spec §15 listet die Log-Files unter ``~/Library/Logs/whatsbot/`` mit Rotation:

==============  =========  =============
File            MaxBytes   Backup-Files
==============  =========  =============
app.jsonl       10 MB      5
hook.jsonl      10 MB      5  (Phase 3)
access.jsonl    10 MB      3  (Phase 3+)
audit.jsonl     50 MB      20 (Phase 4+)
mode-changes    50 MB      20 (Phase 4+)
==============  =========  =============

In Phase 1 wird nur ``app.jsonl`` befüllt. Die übrigen Sinks kommen mit den
Features, die sie benötigen.

Felder pro Event (Spec §15):
``ts, level, logger, msg_id, session_id, project, mode, event, ...payload``

``msg_id``/``session_id``/``project``/``mode`` werden via
``structlog.contextvars`` per-Request gesetzt (siehe
``whatsbot.http.middleware.CorrelationIdMiddleware``).
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Final

import structlog

DEFAULT_LOG_DIR: Final[Path] = Path.home() / "Library" / "Logs" / "whatsbot"

APP_LOG_MAX_BYTES: Final = 10 * 1024 * 1024
APP_LOG_BACKUPS: Final = 5


def configure_logging(
    log_dir: Path = DEFAULT_LOG_DIR,
    level: str = "INFO",
    *,
    write_to_files: bool = True,
    write_to_stderr: bool = True,
) -> None:
    """Configure stdlib logging + structlog. Idempotent — safe to call twice.

    ``write_to_files=False`` is used by tests to keep the FS clean.
    ``write_to_stderr=True`` mirrors output to stderr for ``make run-dev``.
    """
    handlers: list[logging.Handler] = []

    if write_to_stderr:
        handlers.append(logging.StreamHandler(sys.stderr))

    if write_to_files:
        log_dir.mkdir(parents=True, exist_ok=True)
        app_handler = logging.handlers.RotatingFileHandler(
            log_dir / "app.jsonl",
            maxBytes=APP_LOG_MAX_BYTES,
            backupCount=APP_LOG_BACKUPS,
            encoding="utf-8",
        )
        handlers.append(app_handler)

    # The stdlib formatter is bypassed: structlog produces the final JSON
    # string, and the handler just writes it through unchanged.
    logging.basicConfig(
        format="%(message)s",
        level=level,
        handlers=handlers,
        force=True,
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.TimeStamper(fmt="iso", utc=True, key="ts"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a structlog logger bound to the given name."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
