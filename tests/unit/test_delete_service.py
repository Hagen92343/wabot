"""Unit tests for whatsbot.application.delete_service."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_pending_delete_repository import (
    SqlitePendingDeleteRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.delete_service import (
    DeleteService,
    InvalidPinError,
    NoPendingDeleteError,
    PanicPinNotConfiguredError,
    PendingDeleteExpiredError,
)
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.app_state_repository import KEY_ACTIVE_PROJECT
from whatsbot.ports.project_repository import ProjectNotFoundError
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit


class StubSecretsProvider:
    def __init__(self, secrets: dict[str, str]) -> None:
        self._store = dict(secrets)

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


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
def trash_root(tmp_path: Path) -> Path:
    root = tmp_path / "trash"
    root.mkdir()
    return root


@pytest.fixture
def project_repo(conn: sqlite3.Connection) -> SqliteProjectRepository:
    return SqliteProjectRepository(conn)


@pytest.fixture
def app_state(conn: sqlite3.Connection) -> SqliteAppStateRepository:
    return SqliteAppStateRepository(conn)


@pytest.fixture
def pending_repo(conn: sqlite3.Connection) -> SqlitePendingDeleteRepository:
    return SqlitePendingDeleteRepository(conn)


def _seed_project(
    repo: SqliteProjectRepository, projects_root: Path, name: str = "alpha"
) -> None:
    (projects_root / name).mkdir()
    (projects_root / name / "README.md").write_text("x", encoding="utf-8")
    repo.create(
        Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )


def _service(
    *,
    pending_repo: SqlitePendingDeleteRepository,
    project_repo: SqliteProjectRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
    pin: str = "1234",
    clock_values: list[int] | None = None,
) -> DeleteService:
    secrets = StubSecretsProvider({KEY_PANIC_PIN: pin}) if pin else StubSecretsProvider({})
    if clock_values is None:
        clock_callable = lambda: 1_000
    else:
        it = iter(clock_values)
        clock_callable = lambda: next(it)
    return DeleteService(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        secrets=secrets,
        projects_root=projects_root,
        trash_root=trash_root,
        clock=clock_callable,
    )


# --- request_delete --------------------------------------------------------


def test_request_delete_creates_pending_row(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
    )
    pending = svc.request_delete("alpha")
    assert pending.project_name == "alpha"
    assert pending.deadline_ts == 1_060
    assert pending_repo.get("alpha") == pending


def test_request_delete_unknown_project(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
    )
    with pytest.raises(ProjectNotFoundError):
        svc.request_delete("ghost")


def test_request_delete_twice_resets_deadline(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 2_000],
    )
    first = svc.request_delete("alpha")
    second = svc.request_delete("alpha")
    assert first.deadline_ts == 1_060
    assert second.deadline_ts == 2_060
    assert pending_repo.get("alpha") == second


# --- confirm_delete --------------------------------------------------------


def test_confirm_delete_success(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    outcome = svc.confirm_delete("alpha", "1234")
    assert outcome.project_name == "alpha"
    assert outcome.trashed_to.parent == trash_root
    assert outcome.trashed_to.name.startswith("whatsbot-alpha-")
    assert outcome.trashed_to.is_dir()
    # Original gone, row gone, pending row gone.
    assert not (projects_root / "alpha").exists()
    assert not project_repo.exists("alpha")
    assert pending_repo.get("alpha") is None


def test_confirm_delete_without_request(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
    )
    with pytest.raises(NoPendingDeleteError):
        svc.confirm_delete("alpha", "1234")
    # Project must still be intact.
    assert (projects_root / "alpha").exists()
    assert project_repo.exists("alpha")


def test_confirm_delete_expired_cleans_pending_row(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_070],  # request, then confirm 70s later
    )
    svc.request_delete("alpha")
    with pytest.raises(PendingDeleteExpiredError):
        svc.confirm_delete("alpha", "1234")
    # Stale row is cleaned so the next /rm starts fresh.
    assert pending_repo.get("alpha") is None
    assert project_repo.exists("alpha")
    assert (projects_root / "alpha").exists()


def test_confirm_delete_wrong_pin(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    with pytest.raises(InvalidPinError):
        svc.confirm_delete("alpha", "9999")
    # Pending row must stay so the user can retry with the correct PIN.
    assert pending_repo.get("alpha") is not None
    assert project_repo.exists("alpha")
    assert (projects_root / "alpha").exists()


def test_confirm_delete_missing_panic_pin(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        pin="",
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    with pytest.raises(PanicPinNotConfiguredError):
        svc.confirm_delete("alpha", "1234")
    assert project_repo.exists("alpha")


def test_confirm_delete_clears_active_project(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    app_state.set(KEY_ACTIVE_PROJECT, "alpha")
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    svc.confirm_delete("alpha", "1234")
    assert app_state.get(KEY_ACTIVE_PROJECT) is None


def test_confirm_delete_leaves_unrelated_active_project(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root, name="alpha")
    _seed_project(project_repo, projects_root, name="beta")
    app_state.set(KEY_ACTIVE_PROJECT, "beta")
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    svc.confirm_delete("alpha", "1234")
    assert app_state.get(KEY_ACTIVE_PROJECT) == "beta"


def test_confirm_delete_cascades_to_allow_rules(
    conn: sqlite3.Connection,
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    _seed_project(project_repo, projects_root)
    conn.execute(
        "INSERT INTO allow_rules(project_name, tool, pattern, created_at, source) "
        "VALUES ('alpha', 'Bash', 'echo hi', '2026-04-22T12:00:00', 'manual')"
    )
    assert conn.execute(
        "SELECT COUNT(*) FROM allow_rules WHERE project_name = 'alpha'"
    ).fetchone()[0] == 1

    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    svc.confirm_delete("alpha", "1234")
    assert conn.execute(
        "SELECT COUNT(*) FROM allow_rules WHERE project_name = 'alpha'"
    ).fetchone()[0] == 0


def test_confirm_delete_handles_missing_project_dir(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    """Row in DB but dir gone on disk (user deleted manually). Confirm
    still succeeds and leaves a marker dir in trash — the DB row was the
    source of truth."""
    _seed_project(project_repo, projects_root)
    import shutil as _shutil

    _shutil.rmtree(projects_root / "alpha")
    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[1_000, 1_030],
    )
    svc.request_delete("alpha")
    outcome = svc.confirm_delete("alpha", "1234")
    assert not project_repo.exists("alpha")
    assert outcome.trashed_to.parent == trash_root


# --- cleanup_expired -------------------------------------------------------


def test_cleanup_expired_removes_stale_rows(
    project_repo: SqliteProjectRepository,
    pending_repo: SqlitePendingDeleteRepository,
    app_state: SqliteAppStateRepository,
    projects_root: Path,
    trash_root: Path,
) -> None:
    from whatsbot.domain.pending_deletes import PendingDelete

    _seed_project(project_repo, projects_root, name="alpha")
    _seed_project(project_repo, projects_root, name="beta")
    pending_repo.upsert(PendingDelete(project_name="alpha", deadline_ts=500))
    pending_repo.upsert(PendingDelete(project_name="beta", deadline_ts=5_000))

    svc = _service(
        pending_repo=pending_repo,
        project_repo=project_repo,
        app_state=app_state,
        projects_root=projects_root,
        trash_root=trash_root,
        clock_values=[3_000],
    )
    evicted = svc.cleanup_expired()
    assert evicted == ["alpha"]
    assert pending_repo.get("beta") is not None
