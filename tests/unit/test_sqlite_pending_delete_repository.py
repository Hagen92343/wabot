"""Unit tests for whatsbot.adapters.sqlite_pending_delete_repository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_pending_delete_repository import (
    SqlitePendingDeleteRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.domain.pending_deletes import PendingDelete
from whatsbot.domain.projects import Mode, Project, SourceMode
from datetime import UTC, datetime

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqlitePendingDeleteRepository:
    return SqlitePendingDeleteRepository(conn)


def test_get_missing_returns_none(repo: SqlitePendingDeleteRepository) -> None:
    assert repo.get("ghost") is None


def test_upsert_then_get_roundtrip(repo: SqlitePendingDeleteRepository) -> None:
    pending = PendingDelete(project_name="alpha", deadline_ts=1_060)
    repo.upsert(pending)
    fetched = repo.get("alpha")
    assert fetched == pending


def test_upsert_overwrites_existing_deadline(
    repo: SqlitePendingDeleteRepository,
) -> None:
    repo.upsert(PendingDelete(project_name="alpha", deadline_ts=1_060))
    repo.upsert(PendingDelete(project_name="alpha", deadline_ts=2_000))
    assert repo.get("alpha") == PendingDelete(project_name="alpha", deadline_ts=2_000)


def test_delete_returns_true_when_row_existed(
    repo: SqlitePendingDeleteRepository,
) -> None:
    repo.upsert(PendingDelete(project_name="alpha", deadline_ts=1_060))
    assert repo.delete("alpha") is True
    assert repo.get("alpha") is None


def test_delete_returns_false_when_absent(
    repo: SqlitePendingDeleteRepository,
) -> None:
    assert repo.delete("alpha") is False


def test_delete_expired_sweeps_only_stale_rows(
    repo: SqlitePendingDeleteRepository,
) -> None:
    repo.upsert(PendingDelete(project_name="alpha", deadline_ts=1_000))  # expired
    repo.upsert(PendingDelete(project_name="beta", deadline_ts=2_000))   # expired
    repo.upsert(PendingDelete(project_name="gamma", deadline_ts=5_000))  # fresh

    evicted = repo.delete_expired(now_ts=3_000)
    assert sorted(evicted) == ["alpha", "beta"]
    assert repo.get("alpha") is None
    assert repo.get("beta") is None
    assert repo.get("gamma") is not None


def test_delete_expired_empty_returns_empty_list(
    repo: SqlitePendingDeleteRepository,
) -> None:
    assert repo.delete_expired(now_ts=3_000) == []


def test_pending_row_independent_of_projects_table(
    conn: sqlite3.Connection,
    repo: SqlitePendingDeleteRepository,
) -> None:
    """The ``pending_deletes`` schema has no FK to ``projects``. Verifies
    that dropping the project does NOT auto-delete the pending row — the
    sweeper / confirm path is responsible for that."""
    project_repo = SqliteProjectRepository(conn)
    project_repo.create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )
    repo.upsert(PendingDelete(project_name="alpha", deadline_ts=1_060))
    project_repo.delete("alpha")
    assert repo.get("alpha") is not None  # orphan, will be swept separately
