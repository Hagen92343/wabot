"""Unit tests for whatsbot.adapters.sqlite_project_repository.

Run against a fresh in-memory SQLite DB seeded with the schema from
``sql/schema.sql`` so we exercise the real CHECK constraints.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.project_repository import (
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
)

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
def repo(conn: sqlite3.Connection) -> SqliteProjectRepository:
    return SqliteProjectRepository(conn)


def _project(name: str = "alpha", mode: Mode = Mode.NORMAL) -> Project:
    return Project(
        name=name,
        source_mode=SourceMode.EMPTY,
        created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        mode=mode,
    )


# --- create + get + exists -------------------------------------------------


def test_create_then_get_roundtrip(repo: SqliteProjectRepository) -> None:
    p = _project("alpha")
    repo.create(p)
    fetched = repo.get("alpha")
    assert fetched.name == "alpha"
    assert fetched.source_mode is SourceMode.EMPTY
    assert fetched.mode is Mode.NORMAL
    assert fetched.created_at == p.created_at


def test_create_persists_mode(repo: SqliteProjectRepository) -> None:
    repo.create(_project("yoloproj", mode=Mode.YOLO))
    assert repo.get("yoloproj").mode is Mode.YOLO


def test_create_duplicate_raises(repo: SqliteProjectRepository) -> None:
    repo.create(_project("alpha"))
    with pytest.raises(ProjectAlreadyExistsError, match="alpha"):
        repo.create(_project("alpha"))


def test_get_missing_raises(repo: SqliteProjectRepository) -> None:
    with pytest.raises(ProjectNotFoundError, match="ghost"):
        repo.get("ghost")


def test_exists_true_after_create(repo: SqliteProjectRepository) -> None:
    repo.create(_project("alpha"))
    assert repo.exists("alpha") is True


def test_exists_false_for_unknown(repo: SqliteProjectRepository) -> None:
    assert repo.exists("ghost") is False


# --- list ------------------------------------------------------------------


def test_list_empty_returns_empty_list(repo: SqliteProjectRepository) -> None:
    assert repo.list_all() == []


def test_list_returns_alphabetical(repo: SqliteProjectRepository) -> None:
    for name in ("zeta", "alpha", "mu"):
        repo.create(_project(name))
    names = [p.name for p in repo.list_all()]
    assert names == ["alpha", "mu", "zeta"]


# --- delete ----------------------------------------------------------------


def test_delete_removes_row(repo: SqliteProjectRepository) -> None:
    repo.create(_project("alpha"))
    assert repo.exists("alpha")
    repo.delete("alpha")
    assert not repo.exists("alpha")


def test_delete_missing_raises(repo: SqliteProjectRepository) -> None:
    with pytest.raises(ProjectNotFoundError, match="ghost"):
        repo.delete("ghost")


# --- DB CHECK constraints actively enforced --------------------------------


def test_invalid_mode_in_db_rejected_by_check(conn: sqlite3.Connection) -> None:
    """Spec §19: mode CHECK(IN ('normal', 'strict', 'yolo'))."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at, mode) "
            "VALUES (?, 'empty', '2026-01-01', 'rocket')",
            ("p",),
        )


def test_invalid_source_mode_rejected_by_check(conn: sqlite3.Connection) -> None:
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at) "
            "VALUES (?, 'banana', '2026-01-01')",
            ("p",),
        )


# --- path column (Phase 11) ------------------------------------------------


def test_create_persists_path_for_imported(
    repo: SqliteProjectRepository, conn: sqlite3.Connection
) -> None:
    project = Project(
        name="wabot",
        source_mode=SourceMode.IMPORTED,
        created_at=datetime(2026, 4, 24, tzinfo=UTC),
        path=Path("/Users/hagenmarggraf/whatsbot"),
    )
    repo.create(project)
    row = conn.execute(
        "SELECT path, source_mode FROM projects WHERE name = 'wabot'"
    ).fetchone()
    assert row["path"] == "/Users/hagenmarggraf/whatsbot"
    assert row["source_mode"] == "imported"


def test_create_without_path_stores_null(
    repo: SqliteProjectRepository, conn: sqlite3.Connection
) -> None:
    repo.create(_project("legacy"))
    row = conn.execute(
        "SELECT path FROM projects WHERE name = 'legacy'"
    ).fetchone()
    assert row["path"] is None


def test_get_returns_imported_project_with_path(
    repo: SqliteProjectRepository,
) -> None:
    repo.create(
        Project(
            name="wabot",
            source_mode=SourceMode.IMPORTED,
            created_at=datetime(2026, 4, 24, tzinfo=UTC),
            path=Path("/Users/hagenmarggraf/whatsbot"),
        )
    )
    fetched = repo.get("wabot")
    assert fetched.path == Path("/Users/hagenmarggraf/whatsbot")
    assert fetched.source_mode is SourceMode.IMPORTED


def test_exists_with_path_true_when_registered(
    repo: SqliteProjectRepository,
) -> None:
    path = Path("/opt/some/existing/project")
    repo.create(
        Project(
            name="other",
            source_mode=SourceMode.IMPORTED,
            created_at=datetime(2026, 4, 24, tzinfo=UTC),
            path=path,
        )
    )
    assert repo.exists_with_path(path) is True


def test_exists_with_path_false_when_unknown(
    repo: SqliteProjectRepository,
) -> None:
    assert repo.exists_with_path(Path("/never/seen/this/path")) is False


def test_exists_with_path_false_for_legacy_without_path(
    repo: SqliteProjectRepository,
) -> None:
    # Legacy empty project has path=NULL, should never match any query.
    repo.create(_project("legacy"))
    assert repo.exists_with_path(Path("/tmp/whatever")) is False
