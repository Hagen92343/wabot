"""Unit tests for whatsbot.application.force_service (Phase 5 C5.4)."""

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
from whatsbot.application.delete_service import (
    InvalidPinError,
    PanicPinNotConfiguredError,
)
from whatsbot.application.force_service import ForceService
from whatsbot.application.lock_service import LockService
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    Mode,
    Project,
    SourceMode,
)
from whatsbot.ports.project_repository import ProjectNotFoundError
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
PIN = "1234"


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
    SqliteProjectRepository(c).create(
        Project(
            name="alpha",
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
def lock_repo(conn: sqlite3.Connection) -> SqliteSessionLockRepository:
    return SqliteSessionLockRepository(conn)


@pytest.fixture
def lock_service(
    lock_repo: SqliteSessionLockRepository,
) -> LockService:
    return LockService(repo=lock_repo, clock=lambda: NOW)


@pytest.fixture
def project_repo(conn: sqlite3.Connection) -> SqliteProjectRepository:
    return SqliteProjectRepository(conn)


def _service(
    *,
    lock_service: LockService,
    project_repo: SqliteProjectRepository,
    pin: str | None = PIN,
) -> ForceService:
    secrets = StubSecretsProvider(
        {KEY_PANIC_PIN: pin} if pin is not None else {}
    )
    return ForceService(
        lock_service=lock_service,
        project_repo=project_repo,
        secrets=secrets,
    )


# ---- happy path ----------------------------------------------------


def test_force_takes_lock_with_correct_pin(
    lock_service: LockService,
    lock_repo: SqliteSessionLockRepository,
    project_repo: SqliteProjectRepository,
) -> None:
    # Pre-existing local lock that the bot wants to override.
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW,
            last_activity_at=NOW,
        )
    )

    svc = _service(lock_service=lock_service, project_repo=project_repo)
    outcome = svc.force("alpha", PIN)

    assert outcome.project_name == "alpha"
    assert outcome.lock.owner is LockOwner.BOT

    persisted = lock_repo.get("alpha")
    assert persisted is not None
    assert persisted.owner is LockOwner.BOT


def test_force_works_when_no_prior_lock(
    lock_service: LockService,
    lock_repo: SqliteSessionLockRepository,
    project_repo: SqliteProjectRepository,
) -> None:
    """``/force`` is a power tool — it shouldn't insist that a lock
    already exists. Idempotent take-over."""
    svc = _service(lock_service=lock_service, project_repo=project_repo)
    outcome = svc.force("alpha", PIN)
    assert outcome.lock.owner is LockOwner.BOT
    persisted = lock_repo.get("alpha")
    assert persisted is not None
    assert persisted.owner is LockOwner.BOT


# ---- failures: PIN, project, name validation ----------------------


def test_force_rejects_wrong_pin_and_leaves_lock_alone(
    lock_service: LockService,
    lock_repo: SqliteSessionLockRepository,
    project_repo: SqliteProjectRepository,
) -> None:
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW,
            last_activity_at=NOW,
        )
    )

    svc = _service(lock_service=lock_service, project_repo=project_repo)
    with pytest.raises(InvalidPinError):
        svc.force("alpha", "9999")

    # Lock untouched — local terminal still owns it.
    persisted = lock_repo.get("alpha")
    assert persisted is not None
    assert persisted.owner is LockOwner.LOCAL


def test_force_raises_when_panic_pin_not_set(
    lock_service: LockService,
    project_repo: SqliteProjectRepository,
) -> None:
    svc = _service(
        lock_service=lock_service, project_repo=project_repo, pin=None
    )
    with pytest.raises(PanicPinNotConfiguredError):
        svc.force("alpha", "1234")


def test_force_raises_for_unknown_project(
    lock_service: LockService,
    project_repo: SqliteProjectRepository,
) -> None:
    svc = _service(lock_service=lock_service, project_repo=project_repo)
    with pytest.raises(ProjectNotFoundError):
        svc.force("ghost", PIN)


def test_force_raises_for_invalid_project_name(
    lock_service: LockService,
    project_repo: SqliteProjectRepository,
) -> None:
    svc = _service(lock_service=lock_service, project_repo=project_repo)
    with pytest.raises(InvalidProjectNameError):
        svc.force("BAD NAME", PIN)


def test_force_uses_constant_time_pin_compare(
    lock_service: LockService,
    project_repo: SqliteProjectRepository,
) -> None:
    """Same-length PIN with a single mismatched char must still raise.
    Sanity check that we're not accidentally using ``==`` on a substring."""
    svc = _service(lock_service=lock_service, project_repo=project_repo)
    with pytest.raises(InvalidPinError):
        svc.force("alpha", "1235")
    with pytest.raises(InvalidPinError):
        svc.force("alpha", "1234X")  # longer
    with pytest.raises(InvalidPinError):
        svc.force("alpha", "")  # empty
