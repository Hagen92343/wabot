"""C8.1 — SqliteMaxLimitsRepository round-trip tests."""

from __future__ import annotations

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_max_limits_repository import (
    SqliteMaxLimitsRepository,
)
from whatsbot.domain.limits import LimitKind, MaxLimit


@pytest.fixture
def repo():  # type: ignore[no-untyped-def]
    conn = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(conn)
    yield SqliteMaxLimitsRepository(conn)
    conn.close()


def test_get_missing_returns_none(repo) -> None:  # type: ignore[no-untyped-def]
    assert repo.get(LimitKind.SESSION_5H) is None


def test_upsert_and_get_roundtrip(repo) -> None:  # type: ignore[no-untyped-def]
    lim = MaxLimit(
        kind=LimitKind.WEEKLY,
        reset_at_ts=1_700_000_000,
        warned_at_ts=1_699_999_000,
        remaining_pct=0.07,
    )
    repo.upsert(lim)
    got = repo.get(LimitKind.WEEKLY)
    assert got == lim


def test_upsert_replaces_existing(repo) -> None:  # type: ignore[no-untyped-def]
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=1000,
            remaining_pct=0.5,
        )
    )
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=2000,
            remaining_pct=0.1,
        )
    )
    got = repo.get(LimitKind.SESSION_5H)
    assert got is not None
    assert got.reset_at_ts == 2000
    assert got.remaining_pct == pytest.approx(0.1)


def test_mark_warned_partial_update(repo) -> None:  # type: ignore[no-untyped-def]
    repo.upsert(
        MaxLimit(
            kind=LimitKind.OPUS_SUB,
            reset_at_ts=5000,
            warned_at_ts=None,
            remaining_pct=0.05,
        )
    )
    repo.mark_warned(LimitKind.OPUS_SUB, 4000)
    got = repo.get(LimitKind.OPUS_SUB)
    assert got is not None
    assert got.warned_at_ts == 4000
    assert got.reset_at_ts == 5000  # unchanged
    assert got.remaining_pct == pytest.approx(0.05)  # unchanged


def test_mark_warned_on_missing_kind_is_noop(repo) -> None:  # type: ignore[no-untyped-def]
    repo.mark_warned(LimitKind.WEEKLY, 4000)  # no row yet
    assert repo.get(LimitKind.WEEKLY) is None


def test_delete_returns_true_when_row_existed(repo) -> None:  # type: ignore[no-untyped-def]
    repo.upsert(
        MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=1000)
    )
    assert repo.delete(LimitKind.SESSION_5H) is True
    assert repo.get(LimitKind.SESSION_5H) is None


def test_delete_returns_false_when_row_absent(repo) -> None:  # type: ignore[no-untyped-def]
    assert repo.delete(LimitKind.SESSION_5H) is False


def test_list_all_orders_by_reset(repo) -> None:  # type: ignore[no-untyped-def]
    repo.upsert(MaxLimit(kind=LimitKind.WEEKLY, reset_at_ts=3000))
    repo.upsert(MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=1000))
    repo.upsert(MaxLimit(kind=LimitKind.OPUS_SUB, reset_at_ts=2000))
    out = repo.list_all()
    assert [limit.kind for limit in out] == [
        LimitKind.SESSION_5H,
        LimitKind.OPUS_SUB,
        LimitKind.WEEKLY,
    ]


def test_remaining_pct_roundtrip_default(repo) -> None:  # type: ignore[no-untyped-def]
    # default remaining_pct = -1.0 sentinel — persisted + loaded as-is.
    repo.upsert(
        MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=1000)
    )
    got = repo.get(LimitKind.SESSION_5H)
    assert got is not None
    assert got.remaining_pct == pytest.approx(-1.0)


def test_kind_check_constraint_enforced(repo) -> None:  # type: ignore[no-untyped-def]
    """The DB schema only permits the three canonical kinds — direct
    injection of an unknown kind must fail."""
    import sqlite3

    # Our API only accepts LimitKind so we reach below the adapter
    # to test the DB-level constraint.
    with pytest.raises(sqlite3.IntegrityError):
        repo._conn.execute(  # type: ignore[attr-defined]
            "INSERT INTO max_limits(kind, reset_at_ts) VALUES (?, ?)",
            ("bogus", 1000),
        )
