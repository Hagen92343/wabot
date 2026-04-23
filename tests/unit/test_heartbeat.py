"""Pure-domain tests for ``whatsbot.domain.heartbeat`` (Phase 6 C6.4)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from whatsbot.domain.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    HEARTBEAT_STALE_AFTER_SECONDS,
    format_heartbeat_payload,
    is_heartbeat_stale,
)

pytestmark = pytest.mark.unit


# --- staleness -------------------------------------------------------


def test_missing_heartbeat_is_stale() -> None:
    assert is_heartbeat_stale(None, now=1_000.0) is True


def test_fresh_heartbeat_not_stale() -> None:
    assert is_heartbeat_stale(995.0, now=1_000.0) is False


def test_just_below_threshold_not_stale() -> None:
    assert (
        is_heartbeat_stale(
            1_000.0 - HEARTBEAT_STALE_AFTER_SECONDS + 1, now=1_000.0
        )
        is False
    )


def test_at_threshold_is_stale() -> None:
    assert (
        is_heartbeat_stale(
            1_000.0 - HEARTBEAT_STALE_AFTER_SECONDS, now=1_000.0
        )
        is True
    )


def test_custom_threshold() -> None:
    assert is_heartbeat_stale(950.0, now=1_000.0, threshold_seconds=10) is True
    assert is_heartbeat_stale(995.0, now=1_000.0, threshold_seconds=10) is False


# --- payload format -------------------------------------------------


def test_payload_includes_pid_version_and_iso_ts() -> None:
    now = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
    body = format_heartbeat_payload(now=now, pid=1234, version="0.1.0")
    assert "pid=1234" in body
    assert "version=0.1.0" in body
    assert "ts=2026-04-23T12:00:00+00:00" in body


def test_payload_starts_with_human_header() -> None:
    body = format_heartbeat_payload(
        now=datetime(2026, 4, 23, 12, 0, tzinfo=UTC),
        pid=1,
        version="0.1.0",
    )
    assert body.startswith("whatsbot heartbeat\n"), (
        "humans cat-debugging the file should see what it is on line 1"
    )


# --- constants are sane --------------------------------------------


def test_default_interval_below_default_stale_threshold() -> None:
    """If we wrote less often than the watchdog times out, the bot
    would self-trigger the watchdog. ``2x`` margin is comfortable."""
    assert HEARTBEAT_INTERVAL_SECONDS * 2 <= HEARTBEAT_STALE_AFTER_SECONDS
