"""Unit tests for ``whatsbot.application.lockdown_service`` (Phase 6 C6.2)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.domain.lockdown import (
    LOCKDOWN_REASON_PANIC,
    LOCKDOWN_REASON_WATCHDOG,
)
from whatsbot.ports.app_state_repository import KEY_LOCKDOWN

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def marker(tmp_path: Path) -> Path:
    return tmp_path / "whatsbot-PANIC"


@pytest.fixture
def svc(
    conn: sqlite3.Connection, marker: Path
) -> LockdownService:
    return LockdownService(
        app_state=SqliteAppStateRepository(conn),
        panic_marker_path=marker,
        clock=lambda: NOW,
    )


# ---- read --------------------------------------------------------


def test_current_returns_disengaged_when_no_row(
    svc: LockdownService,
) -> None:
    state = svc.current()
    assert state.engaged is False


def test_is_engaged_returns_false_initially(svc: LockdownService) -> None:
    assert svc.is_engaged() is False


# ---- engage ------------------------------------------------------


def test_engage_writes_db_and_marker(
    svc: LockdownService, marker: Path, conn: sqlite3.Connection
) -> None:
    state = svc.engage(reason=LOCKDOWN_REASON_PANIC, engaged_by="panic")
    assert state.engaged is True
    assert state.reason == LOCKDOWN_REASON_PANIC
    assert state.engaged_at == NOW
    assert marker.exists()
    raw = SqliteAppStateRepository(conn).get(KEY_LOCKDOWN)
    assert raw is not None
    assert "panic" in raw


def test_is_engaged_true_after_engage(svc: LockdownService) -> None:
    svc.engage(reason=LOCKDOWN_REASON_PANIC)
    assert svc.is_engaged() is True


def test_engage_is_idempotent_keeps_first_metadata(
    svc: LockdownService,
) -> None:
    """A second engage call after panic shouldn't overwrite the
    panic timestamp / reason — forensics-relevant."""
    first = svc.engage(reason=LOCKDOWN_REASON_PANIC, engaged_by="panic")
    # Build a second service with a later clock, same state store —
    # if engage rewrote things we'd see the new clock.
    later = datetime(2026, 4, 23, 13, 0, tzinfo=UTC)
    svc2 = LockdownService(
        app_state=svc._app_state,  # type: ignore[attr-defined]
        panic_marker_path=svc._marker,  # type: ignore[attr-defined]
        clock=lambda: later,
    )
    second = svc2.engage(
        reason=LOCKDOWN_REASON_WATCHDOG, engaged_by="watchdog"
    )
    assert second.engaged_at == first.engaged_at  # unchanged
    assert second.reason == LOCKDOWN_REASON_PANIC  # unchanged


def test_engage_marker_failure_does_not_block_db_write(
    svc: LockdownService, conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """If the touch-file write blows up, the DB row must still
    persist — the DB is the bot's source of truth."""
    # Create a service whose marker path is unwritable: a dir that
    # already exists where we'd want a file.
    bad_marker = tmp_path / "blocked"
    bad_marker.mkdir()
    bad_svc = LockdownService(
        app_state=SqliteAppStateRepository(conn),
        panic_marker_path=bad_marker,
        clock=lambda: NOW,
    )
    # ``touch(exist_ok=True)`` on a directory raises IsADirectoryError
    # — which is OSError. The service must swallow + log.
    bad_svc.engage(reason=LOCKDOWN_REASON_PANIC)
    assert bad_svc.is_engaged() is True


# ---- disengage --------------------------------------------------


def test_disengage_removes_db_row_and_marker(
    svc: LockdownService, marker: Path, conn: sqlite3.Connection
) -> None:
    svc.engage(reason=LOCKDOWN_REASON_PANIC)
    assert marker.exists()
    state = svc.disengage()
    assert state.engaged is False
    assert not marker.exists()
    raw = SqliteAppStateRepository(conn).get(KEY_LOCKDOWN)
    # Disengage writes a "{engaged: false}" blob, doesn't delete the
    # row — which is what current() expects to keep working.
    assert raw is not None
    assert '"engaged":false' in raw


def test_disengage_is_idempotent_when_already_clear(
    svc: LockdownService,
) -> None:
    state = svc.disengage()
    assert state.engaged is False
    state2 = svc.disengage()
    assert state2.engaged is False


# ---- serialization tolerance ----------------------------------


def test_current_tolerates_garbled_db_row(
    svc: LockdownService, conn: sqlite3.Connection
) -> None:
    """If something writes garbage into the lockdown row, fail-safe
    on disengaged so the bot can keep working."""
    SqliteAppStateRepository(conn).set(KEY_LOCKDOWN, "not-json{")
    state = svc.current()
    assert state.engaged is False


def test_current_tolerates_partial_row(
    svc: LockdownService, conn: sqlite3.Connection
) -> None:
    """Old bot version wrote a row without engaged_at — fall back
    to a minimal engaged state."""
    SqliteAppStateRepository(conn).set(
        KEY_LOCKDOWN, '{"engaged":true}'
    )
    state = svc.current()
    assert state.engaged is True
    assert state.engaged_at is None
