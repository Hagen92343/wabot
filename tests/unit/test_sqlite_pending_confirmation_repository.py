"""Unit tests for SqlitePendingConfirmationRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_pending_confirmation_repository import (
    SqlitePendingConfirmationRepository,
)
from whatsbot.domain.pending_confirmations import (
    ConfirmationKind,
    PendingConfirmation,
    compute_deadline,
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
def repo(conn: sqlite3.Connection) -> SqlitePendingConfirmationRepository:
    return SqlitePendingConfirmationRepository(conn)


def _sample(
    id_: str = "01HQW1",
    kind: ConfirmationKind = ConfirmationKind.HOOK_BASH,
    project: str | None = "alpha",
    payload: str = '{"command": "rm -rf /"}',
    deadline_ts: int = 1_000_300,
    created_at: datetime | None = None,
    msg_id: str | None = "msg01",
) -> PendingConfirmation:
    return PendingConfirmation(
        id=id_,
        kind=kind,
        payload=payload,
        deadline_ts=deadline_ts,
        created_at=created_at or datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        project_name=project,
        msg_id=msg_id,
    )


class TestCreateAndGet:
    def test_get_missing_returns_none(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        assert repo.get("ghost") is None

    def test_create_then_get_roundtrip(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        c = _sample()
        repo.create(c)
        fetched = repo.get(c.id)
        assert fetched == c

    def test_duplicate_id_raises(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        repo.create(_sample())
        with pytest.raises(sqlite3.IntegrityError):
            repo.create(_sample())

    def test_roundtrip_preserves_null_project_and_msg_id(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        c = _sample(project=None, msg_id=None)
        repo.create(c)
        fetched = repo.get(c.id)
        assert fetched == c

    def test_kind_enum_roundtrip(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        c = _sample(id_="01HQW2", kind=ConfirmationKind.HOOK_WRITE)
        repo.create(c)
        fetched = repo.get(c.id)
        assert fetched is not None
        assert fetched.kind is ConfirmationKind.HOOK_WRITE


class TestResolve:
    def test_resolve_deletes_and_returns_true(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        c = _sample()
        repo.create(c)
        assert repo.resolve(c.id) is True
        assert repo.get(c.id) is None

    def test_resolve_absent_returns_false(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        assert repo.resolve("ghost") is False


class TestListOpen:
    def test_empty_returns_empty_list(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        assert repo.list_open() == []

    def test_ordering_is_oldest_first(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        t0 = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        t1 = datetime(2026, 4, 22, 12, 5, tzinfo=UTC)
        t2 = datetime(2026, 4, 22, 12, 10, tzinfo=UTC)
        repo.create(_sample(id_="02", created_at=t1))
        repo.create(_sample(id_="03", created_at=t2))
        repo.create(_sample(id_="01", created_at=t0))
        ids = [c.id for c in repo.list_open()]
        assert ids == ["01", "02", "03"]


class TestDeleteExpired:
    def test_sweep_removes_only_stale_rows(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        repo.create(_sample(id_="old1", deadline_ts=1_000))
        repo.create(_sample(id_="old2", deadline_ts=2_000))
        repo.create(_sample(id_="fresh", deadline_ts=5_000))

        evicted = repo.delete_expired(now_ts=3_000)
        assert sorted(evicted) == ["old1", "old2"]
        assert repo.get("old1") is None
        assert repo.get("fresh") is not None

    def test_delete_expired_empty_returns_empty_list(
        self, repo: SqlitePendingConfirmationRepository
    ) -> None:
        assert repo.delete_expired(now_ts=3_000) == []


class TestDomainHelpers:
    def test_is_expired(self) -> None:
        c = _sample(deadline_ts=1_000)
        assert c.is_expired(1_000) is True
        assert c.is_expired(999) is False

    def test_seconds_left_clamps_to_zero(self) -> None:
        c = _sample(deadline_ts=1_000)
        assert c.seconds_left(500) == 500
        assert c.seconds_left(1_500) == 0

    def test_compute_deadline_default_is_five_minutes(self) -> None:
        assert compute_deadline(1000) == 1000 + 300

    def test_compute_deadline_rejects_negative_window(self) -> None:
        with pytest.raises(ValueError):
            compute_deadline(1000, window=-1)
