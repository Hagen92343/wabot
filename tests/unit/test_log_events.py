"""Unit tests for whatsbot.domain.log_events — pure parsing /
filtering helpers used by Phase 8 C8.2 diagnostics commands."""

from __future__ import annotations

import json

from whatsbot.domain.log_events import (
    ERROR_LEVELS,
    LogEntry,
    filter_by_msg_id,
    filter_errors,
    parse_log_line,
)


def _line(**fields: object) -> str:
    return json.dumps(fields)


def test_parse_valid_line_populates_all_known_fields() -> None:
    line = _line(
        ts="2026-04-21T14:32:11.234Z",
        level="INFO",
        logger="whatsbot.router",
        event="command_routed",
        msg_id="01HQWX",
        project="alpha",
        mode="normal",
        latency_ms=42,
    )
    entry = parse_log_line(line)
    assert entry is not None
    assert entry.ts == "2026-04-21T14:32:11.234Z"
    assert entry.level == "INFO"
    assert entry.logger == "whatsbot.router"
    assert entry.event == "command_routed"
    assert entry.msg_id == "01HQWX"
    assert entry.project == "alpha"
    assert entry.mode == "normal"
    # Extras survive in raw.
    assert entry.raw["latency_ms"] == 42


def test_parse_accepts_timestamp_key_alias() -> None:
    # structlog.TimeStamper writes ``ts`` by default in this project,
    # but a generic ``timestamp`` key is the python-logging default —
    # we accept either.
    entry = parse_log_line(_line(timestamp="2026-04-21", level="ERROR"))
    assert entry is not None
    assert entry.ts == "2026-04-21"


def test_parse_returns_none_for_blank_or_malformed_lines() -> None:
    assert parse_log_line("") is None
    assert parse_log_line("   \n") is None
    assert parse_log_line("not json") is None
    # JSON but not an object:
    assert parse_log_line("[1, 2, 3]") is None
    assert parse_log_line("\"just a string\"") is None


def test_parse_tolerates_missing_fields() -> None:
    entry = parse_log_line(_line(event="startup"))
    assert entry is not None
    assert entry.event == "startup"
    assert entry.ts == ""
    assert entry.level == ""
    assert entry.msg_id is None


def test_parse_coerces_non_string_optionals_to_str() -> None:
    # A numeric msg_id shouldn't crash the parser.
    entry = parse_log_line(_line(event="x", msg_id=12345))
    assert entry is not None
    assert entry.msg_id == "12345"


def test_is_error_matches_all_error_levels() -> None:
    for lvl in ("error", "ERROR", "warning", "Warning", "critical"):
        assert LogEntry(level=lvl).is_error, lvl
    assert not LogEntry(level="INFO").is_error
    assert not LogEntry(level="").is_error


def test_error_levels_constant_is_frozen() -> None:
    # Regression guard: modifying this set in prod code would
    # surprise `/errors` severely.
    assert isinstance(ERROR_LEVELS, frozenset)
    assert "error" in ERROR_LEVELS
    assert "warning" in ERROR_LEVELS


def test_filter_by_msg_id_exact_match_only() -> None:
    entries = [
        LogEntry(msg_id="a", event="x"),
        LogEntry(msg_id="b", event="y"),
        LogEntry(msg_id=None, event="z"),
        LogEntry(msg_id="a", event="q"),
    ]
    out = filter_by_msg_id(entries, "a")
    assert [e.event for e in out] == ["x", "q"]


def test_filter_by_msg_id_empty_target_returns_empty() -> None:
    entries = [LogEntry(msg_id="a"), LogEntry(msg_id="")]
    assert filter_by_msg_id(entries, "") == []
    assert filter_by_msg_id(entries, "   ") == []


def test_filter_errors_keeps_warnings_and_errors_only() -> None:
    entries = [
        LogEntry(level="INFO", event="ok"),
        LogEntry(level="WARNING", event="warn"),
        LogEntry(level="ERROR", event="boom"),
        LogEntry(level="DEBUG", event="noise"),
        LogEntry(level="critical", event="crit"),
    ]
    out = filter_errors(entries)
    assert [e.event for e in out] == ["warn", "boom", "crit"]
