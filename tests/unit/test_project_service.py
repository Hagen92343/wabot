"""Unit tests for whatsbot.application.project_service."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.project_service import (
    ProjectFilesystemError,
    ProjectService,
)
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    Mode,
    SourceMode,
)
from whatsbot.ports.project_repository import ProjectAlreadyExistsError

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
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


@pytest.fixture
def service(conn: sqlite3.Connection, projects_root: Path) -> ProjectService:
    return ProjectService(
        repository=SqliteProjectRepository(conn),
        conn=conn,
        projects_root=projects_root,
    )


# --- create_empty: happy path ----------------------------------------------


def test_create_empty_persists_row(service: ProjectService) -> None:
    project = service.create_empty("alpha")
    assert project.name == "alpha"
    assert project.source_mode is SourceMode.EMPTY
    assert project.mode is Mode.NORMAL
    assert project.created_at is not None


def test_create_empty_creates_directory_with_whatsbot_subdir(
    service: ProjectService, projects_root: Path
) -> None:
    service.create_empty("alpha")
    assert (projects_root / "alpha").is_dir()
    assert (projects_root / "alpha" / ".whatsbot").is_dir()
    assert (projects_root / "alpha" / ".whatsbot" / "outputs").is_dir()


def test_create_empty_appears_in_list(service: ProjectService) -> None:
    service.create_empty("alpha")
    listings = service.list_all()
    assert len(listings) == 1
    assert listings[0].project.name == "alpha"
    assert listings[0].is_active is False


def test_list_marks_active_project(service: ProjectService) -> None:
    service.create_empty("alpha")
    service.create_empty("beta")
    listings = service.list_all(active_name="beta")
    flags = {entry.project.name: entry.is_active for entry in listings}
    assert flags == {"alpha": False, "beta": True}


# --- create_empty: error paths ---------------------------------------------


def test_create_empty_rejects_invalid_name(service: ProjectService) -> None:
    with pytest.raises(InvalidProjectNameError):
        service.create_empty("INVALID NAME")


def test_create_empty_rejects_duplicate(service: ProjectService) -> None:
    service.create_empty("alpha")
    with pytest.raises(ProjectAlreadyExistsError, match="alpha"):
        service.create_empty("alpha")


def test_create_empty_rejects_when_dir_already_exists(
    service: ProjectService, projects_root: Path
) -> None:
    """A leftover directory without a DB row should NOT be silently
    overwritten — that would clobber whatever the user put there."""
    (projects_root / "leftover").mkdir()
    with pytest.raises(ProjectFilesystemError, match="leftover"):
        service.create_empty("leftover")


def test_create_empty_rolls_back_dir_when_db_insert_fails(
    service: ProjectService, projects_root: Path, conn: sqlite3.Connection
) -> None:
    """If the INSERT explodes mid-flight (e.g. CHECK constraint), the
    freshly-created directory must be removed so the next attempt isn't
    blocked by a stale leftover."""
    # Sabotage the INSERT by dropping the table — repo.create will raise
    # an OperationalError, which the service propagates after rollback.
    conn.execute("DROP TABLE projects")
    with pytest.raises(sqlite3.OperationalError):
        service.create_empty("alpha")
    # Rollback should have removed the directory.
    assert not (projects_root / "alpha").exists()


# --- list_all: empty -------------------------------------------------------


def test_list_all_empty(service: ProjectService) -> None:
    assert service.list_all() == []
