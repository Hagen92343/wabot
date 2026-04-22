"""Unit tests for SqlitePendingOutputRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_pending_output_repository import (
    SqlitePendingOutputRepository,
)
from whatsbot.domain.pending_outputs import (
    PendingOutput,
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
def repo(conn: sqlite3.Connection) -> SqlitePendingOutputRepository:
    return SqlitePendingOutputRepository(conn)


def _sample(
    msg_id: str = "01HQW1",
    project: str = "alpha",
    path: str = "/tmp/01HQW1.md",
    size: int = 20_000,
    deadline_ts: int = 2_000_000,
    created_at: datetime | None = None,
) -> PendingOutput:
    return PendingOutput(
        msg_id=msg_id,
        project_name=project,
        output_path=path,
        size_bytes=size,
        created_at=created_at or datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        deadline_ts=deadline_ts,
    )


class TestCreateAndGet:
    def test_missing_returns_none(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        assert repo.get("ghost") is None

    def test_roundtrip(self, repo: SqlitePendingOutputRepository) -> None:
        o = _sample()
        repo.create(o)
        assert repo.get(o.msg_id) == o

    def test_duplicate_id_raises(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        repo.create(_sample())
        with pytest.raises(sqlite3.IntegrityError):
            repo.create(_sample())


class TestLatestOpen:
    def test_empty_returns_none(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        assert repo.latest_open() is None

    def test_picks_newest_by_created_at(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        t0 = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
        t1 = datetime(2026, 4, 22, 12, 5, tzinfo=UTC)
        t2 = datetime(2026, 4, 22, 12, 10, tzinfo=UTC)
        repo.create(_sample(msg_id="a", created_at=t0))
        repo.create(_sample(msg_id="b", created_at=t2))
        repo.create(_sample(msg_id="c", created_at=t1))
        latest = repo.latest_open()
        assert latest is not None
        assert latest.msg_id == "b"


class TestResolve:
    def test_resolve_deletes(self, repo: SqlitePendingOutputRepository) -> None:
        o = _sample()
        repo.create(o)
        assert repo.resolve(o.msg_id) is True
        assert repo.get(o.msg_id) is None

    def test_resolve_absent_returns_false(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        assert repo.resolve("ghost") is False


class TestDeleteExpired:
    def test_sweeps_stale_only(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        repo.create(_sample(msg_id="old1", deadline_ts=1_000))
        repo.create(_sample(msg_id="old2", deadline_ts=2_000))
        repo.create(_sample(msg_id="fresh", deadline_ts=10_000))
        evicted = repo.delete_expired(now_ts=5_000)
        assert sorted(evicted) == ["old1", "old2"]
        assert repo.get("fresh") is not None

    def test_empty_returns_empty(
        self, repo: SqlitePendingOutputRepository
    ) -> None:
        assert repo.delete_expired(now_ts=5_000) == []


class TestDomainHelpers:
    def test_is_expired(self) -> None:
        assert _sample(deadline_ts=1_000).is_expired(1_000) is True
        assert _sample(deadline_ts=1_000).is_expired(999) is False

    def test_compute_deadline_default_is_24h(self) -> None:
        assert compute_deadline(0) == 24 * 3600

    def test_compute_deadline_rejects_negative_window(self) -> None:
        with pytest.raises(ValueError):
            compute_deadline(0, window=-1)
