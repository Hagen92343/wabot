"""Unit tests for SqliteSessionLockRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.projects import Mode, Project, SourceMode

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    # FK to projects — seed one.
    SqliteProjectRepository(c).create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteSessionLockRepository:
    return SqliteSessionLockRepository(conn)


def _lock(owner: LockOwner) -> SessionLock:
    return SessionLock(
        project_name="alpha",
        owner=owner,
        acquired_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        last_activity_at=datetime(2026, 4, 22, 12, 5, tzinfo=UTC),
    )


def test_get_missing_returns_none(repo: SqliteSessionLockRepository) -> None:
    assert repo.get("alpha") is None


def test_upsert_then_get_roundtrip(
    repo: SqliteSessionLockRepository,
) -> None:
    lock = _lock(LockOwner.BOT)
    repo.upsert(lock)
    got = repo.get("alpha")
    assert got == lock


def test_upsert_overwrites_existing(
    repo: SqliteSessionLockRepository,
) -> None:
    repo.upsert(_lock(LockOwner.BOT))
    repo.upsert(_lock(LockOwner.LOCAL))
    got = repo.get("alpha")
    assert got is not None
    assert got.owner is LockOwner.LOCAL


def test_delete_returns_true_when_row_existed(
    repo: SqliteSessionLockRepository,
) -> None:
    repo.upsert(_lock(LockOwner.BOT))
    assert repo.delete("alpha") is True
    assert repo.get("alpha") is None


def test_delete_returns_false_when_missing(
    repo: SqliteSessionLockRepository,
) -> None:
    assert repo.delete("alpha") is False


def test_list_all_empty(repo: SqliteSessionLockRepository) -> None:
    assert repo.list_all() == []


def test_list_all_sorted_by_project_name(
    conn: sqlite3.Connection, repo: SqliteSessionLockRepository
) -> None:
    SqliteProjectRepository(conn).create(
        Project(
            name="beta",
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )
    repo.upsert(_lock(LockOwner.BOT))
    repo.upsert(
        SessionLock(
            project_name="beta",
            owner=LockOwner.LOCAL,
            acquired_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            last_activity_at=datetime(2026, 4, 22, 12, 1, tzinfo=UTC),
        )
    )
    names = [lock.project_name for lock in repo.list_all()]
    assert names == ["alpha", "beta"]


def test_check_constraint_rejects_invalid_owner(
    repo: SqliteSessionLockRepository,
) -> None:
    """Schema-level defence: the CHECK constraint in ``session_locks``
    rejects any owner string that isn't 'bot'/'local'/'free'. Our
    ``LockOwner`` enum already makes this unreachable from typed
    code — this test pins the schema itself."""
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(
            "INSERT INTO session_locks(project_name, owner, "
            "acquired_at, last_activity_at) VALUES (?, ?, ?, ?)",
            ("alpha", "ghost", 0, 0),
        )
