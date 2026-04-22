"""Unit tests for whatsbot.domain.commands."""

from __future__ import annotations

import pytest

from whatsbot.domain.commands import StatusSnapshot, route

pytestmark = pytest.mark.unit


def _snap(**overrides: object) -> StatusSnapshot:
    base = {"version": "0.1.0", "uptime_seconds": 12.7, "db_ok": True, "env": "test"}
    base.update(overrides)
    return StatusSnapshot(**base)  # type: ignore[arg-type]


def test_ping_returns_pong_with_version_and_rounded_uptime() -> None:
    result = route("/ping", _snap(version="0.1.0", uptime_seconds=12.7))
    assert result.command == "/ping"
    assert "pong" in result.reply
    assert "v0.1.0" in result.reply
    assert "13s" in result.reply  # round(12.7)


def test_status_includes_env_uptime_and_db_marker() -> None:
    result = route("/status", _snap(env="prod", uptime_seconds=42.0, db_ok=True))
    assert result.command == "/status"
    assert "prod" in result.reply
    assert "42s" in result.reply
    assert "ok" in result.reply.lower()


def test_status_marks_db_degraded_when_unhealthy() -> None:
    result = route("/status", _snap(db_ok=False))
    assert "DEGRADED" in result.reply


def test_help_lists_all_phase_one_commands() -> None:
    result = route("/help", _snap())
    assert result.command == "/help"
    for cmd in ("/ping", "/status", "/help"):
        assert cmd in result.reply


def test_unknown_command_returns_friendly_hint() -> None:
    result = route("/unknown-thing", _snap())
    assert result.command == "<unknown>"
    assert "/help" in result.reply


def test_leading_and_trailing_whitespace_is_ignored() -> None:
    assert route("  /ping  ", _snap()).command == "/ping"
    assert route("\t/help\n", _snap()).command == "/help"


def test_command_routing_is_case_sensitive() -> None:
    """Spec §11 lists commands lower-case. /PING is unknown by design."""
    assert route("/PING", _snap()).command == "<unknown>"


def test_empty_text_is_unknown() -> None:
    assert route("", _snap()).command == "<unknown>"
    assert route("   ", _snap()).command == "<unknown>"
