"""Unit tests for whatsbot.application.lock_service."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.application.lock_service import (
    LocalTerminalHoldsLockError,
    LockService,
)
from whatsbot.domain.locks import (
    LOCK_TIMEOUT_SECONDS,
    AcquireOutcome,
    LockOwner,
    SessionLock,
)
from whatsbot.domain.projects import Mode, Project, SourceMode

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


class _Clock:
    """Mutable clock so tests can step time forward."""

    def __init__(self, start: datetime) -> None:
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now = self._now + timedelta(seconds=seconds)


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    # FK → projects. Seed two so multi-project tests work.
    project_repo = SqliteProjectRepository(c)
    for name in ("alpha", "beta"):
        project_repo.create(
            Project(
                name=name,
                source_mode=SourceMode.EMPTY,
                created_at=NOW,
                mode=Mode.NORMAL,
            )
        )
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def clock() -> _Clock:
    return _Clock(NOW)


@pytest.fixture
def svc(conn: sqlite3.Connection, clock: _Clock) -> LockService:
    return LockService(repo=SqliteSessionLockRepository(conn), clock=clock)


# ---- acquire_for_bot --------------------------------------------------


def test_acquire_on_clean_project_grants(svc: LockService) -> None:
    result = svc.acquire_for_bot("alpha")
    assert result.outcome is AcquireOutcome.GRANTED
    assert result.lock.owner is LockOwner.BOT


def test_acquire_twice_grants_twice_and_refreshes_activity(
    svc: LockService, clock: _Clock
) -> None:
    svc.acquire_for_bot("alpha")
    clock.advance(30)
    result = svc.acquire_for_bot("alpha")
    assert result.outcome is AcquireOutcome.GRANTED
    assert result.lock.last_activity_at == clock()


def test_acquire_raises_when_local_holds_active(
    svc: LockService, clock: _Clock
) -> None:
    svc.note_local_input("alpha")
    clock.advance(5)  # still well within timeout
    with pytest.raises(LocalTerminalHoldsLockError) as excinfo:
        svc.acquire_for_bot("alpha")
    assert excinfo.value.project_name == "alpha"


def test_acquire_auto_releases_stale_local(
    svc: LockService, clock: _Clock
) -> None:
    svc.note_local_input("alpha")
    clock.advance(LOCK_TIMEOUT_SECONDS + 1)
    result = svc.acquire_for_bot("alpha")
    assert result.outcome is AcquireOutcome.AUTO_RELEASED_THEN_GRANTED
    assert result.lock.owner is LockOwner.BOT


# ---- note_local_input -------------------------------------------------


def test_note_local_input_on_empty_creates_local_lock(
    svc: LockService,
) -> None:
    lock = svc.note_local_input("alpha")
    assert lock.owner is LockOwner.LOCAL


def test_note_local_input_preempts_bot(
    svc: LockService, clock: _Clock
) -> None:
    svc.acquire_for_bot("alpha")
    clock.advance(5)
    lock = svc.note_local_input("alpha")
    assert lock.owner is LockOwner.LOCAL


def test_note_local_refresh_keeps_acquired_at_stable(
    svc: LockService, clock: _Clock
) -> None:
    lock1 = svc.note_local_input("alpha")
    clock.advance(10)
    lock2 = svc.note_local_input("alpha")
    assert lock1.acquired_at == lock2.acquired_at
    assert lock2.last_activity_at == clock()


# ---- force_bot --------------------------------------------------------


def test_force_bot_overwrites_active_local(
    svc: LockService, clock: _Clock
) -> None:
    svc.note_local_input("alpha")
    clock.advance(5)
    forced = svc.force_bot("alpha")
    assert forced.owner is LockOwner.BOT
    # acquire_for_bot would now succeed because the row is bot-owned.
    result = svc.acquire_for_bot("alpha")
    assert result.outcome is AcquireOutcome.GRANTED


# ---- release ----------------------------------------------------------


def test_release_removes_row(svc: LockService) -> None:
    svc.acquire_for_bot("alpha")
    assert svc.release("alpha") is True
    assert svc.current("alpha") is None


def test_release_missing_project_is_false(svc: LockService) -> None:
    assert svc.release("ghost") is False


# ---- sweep_expired ----------------------------------------------------


def test_sweep_reaps_stale_local_only(
    svc: LockService, clock: _Clock
) -> None:
    svc.note_local_input("alpha")
    svc.acquire_for_bot("beta")  # bot-owned — never reaped
    clock.advance(LOCK_TIMEOUT_SECONDS + 1)
    reaped = svc.sweep_expired()
    assert reaped == ["alpha"]
    # beta's lock survived.
    lock = svc.current("beta")
    assert lock is not None
    assert lock.owner is LockOwner.BOT


def test_sweep_empty_when_nothing_stale(
    svc: LockService, clock: _Clock
) -> None:
    svc.note_local_input("alpha")
    clock.advance(5)  # not yet timeout
    assert svc.sweep_expired() == []


# ---- multi-project isolation -----------------------------------------


def test_lock_state_isolated_per_project(svc: LockService) -> None:
    svc.note_local_input("alpha")
    result = svc.acquire_for_bot("beta")
    assert result.outcome is AcquireOutcome.GRANTED
    # alpha's local lock is untouched.
    alpha = svc.current("alpha")
    assert alpha is not None
    assert alpha.owner is LockOwner.LOCAL


# ---- current ----------------------------------------------------------


def test_current_returns_none_for_fresh_project(svc: LockService) -> None:
    assert svc.current("alpha") is None


def test_current_returns_lock_after_acquire(svc: LockService) -> None:
    svc.acquire_for_bot("alpha")
    lock = svc.current("alpha")
    assert lock is not None
    assert lock.owner is LockOwner.BOT


# ---- custom clock + typing sanity ------------------------------------


def test_custom_clock_plumbed_through(
    conn: sqlite3.Connection,
) -> None:
    """mypy-level + runtime check that the clock hook is honored."""
    fixed = datetime(2026, 1, 1, tzinfo=UTC)
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: fixed,
        timeout_seconds=LOCK_TIMEOUT_SECONDS,
    )
    result = svc.acquire_for_bot("alpha")
    assert result.lock.acquired_at == fixed


def _unused(x: SessionLock) -> None:
    """Silences an unused-import linter warning — SessionLock is
    re-exported for test file consumers that want to build their
    own locks without re-plumbing the import."""
    del x
