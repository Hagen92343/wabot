"""Unit tests for whatsbot.logging_setup."""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

import pytest
import structlog

from whatsbot.logging_setup import (
    APP_LOG_BACKUPS,
    APP_LOG_MAX_BYTES,
    configure_logging,
    get_logger,
)

pytestmark = pytest.mark.unit


def _read_last_log_line(log_dir: Path) -> dict[str, object]:
    log_file = log_dir / "app.jsonl"
    # Flush all handlers so the file content is current.
    for handler in logging.getLogger().handlers:
        handler.flush()
    text = log_file.read_text(encoding="utf-8").strip()
    assert text, f"app.jsonl in {log_dir} is empty"
    return json.loads(text.splitlines()[-1])  # type: ignore[no-any-return]


def test_configure_logging_writes_json_with_required_fields(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path, write_to_stderr=False, write_to_files=True)
    log = get_logger("whatsbot.test")
    log.info("startup_complete", env="test", version="0.1.0")

    payload = _read_last_log_line(tmp_path)
    assert payload["event"] == "startup_complete"
    assert payload["env"] == "test"
    assert payload["version"] == "0.1.0"
    assert payload["level"] == "info"
    assert payload["logger"] == "whatsbot.test"
    assert "ts" in payload  # ISO-8601 timestamp added by TimeStamper


def test_configure_logging_merges_contextvars(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path, write_to_stderr=False, write_to_files=True)
    log = get_logger("whatsbot.ctx")

    structlog.contextvars.bind_contextvars(
        msg_id="01ABCDEFG",
        project="alpha",
        mode="normal",
    )
    try:
        log.info("with_request_context")
    finally:
        structlog.contextvars.clear_contextvars()

    payload = _read_last_log_line(tmp_path)
    assert payload["msg_id"] == "01ABCDEFG"
    assert payload["project"] == "alpha"
    assert payload["mode"] == "normal"


def test_rotating_handler_uses_spec_section_15_limits(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path, write_to_stderr=False, write_to_files=True)
    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert len(rotating) == 1
    handler = rotating[0]
    assert handler.maxBytes == APP_LOG_MAX_BYTES == 10 * 1024 * 1024
    assert handler.backupCount == APP_LOG_BACKUPS == 5
    # File-name lives under log_dir
    assert Path(handler.baseFilename).parent == tmp_path
    assert Path(handler.baseFilename).name == "app.jsonl"


def test_configure_logging_creates_log_dir(tmp_path: Path) -> None:
    nested = tmp_path / "deeply" / "nested" / "logs"
    assert not nested.exists()
    configure_logging(log_dir=nested, write_to_stderr=False, write_to_files=True)
    assert nested.is_dir()


def test_configure_logging_disables_files_when_requested(tmp_path: Path) -> None:
    configure_logging(log_dir=tmp_path, write_to_stderr=False, write_to_files=False)
    rotating = [
        h
        for h in logging.getLogger().handlers
        if isinstance(h, logging.handlers.RotatingFileHandler)
    ]
    assert rotating == []


def test_get_logger_returns_named_logger() -> None:
    configure_logging(write_to_files=False, write_to_stderr=False)
    log = get_logger("named.scope")
    # structlog BoundLogger exposes the bound name via _logger.name on stdlib
    bound_name = getattr(log, "name", None) or getattr(getattr(log, "_logger", None), "name", None)
    assert bound_name == "named.scope"
