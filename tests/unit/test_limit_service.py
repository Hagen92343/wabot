"""C8.1 — LimitService behavioural tests."""

from __future__ import annotations

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_max_limits_repository import (
    SqliteMaxLimitsRepository,
)
from whatsbot.application.limit_service import (
    LimitService,
    MaxLimitActiveError,
)
from whatsbot.domain.limits import LimitKind, MaxLimit
from whatsbot.domain.transcript import UsageLimitEvent


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


@pytest.fixture
def conn():  # type: ignore[no-untyped-def]
    conn = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(conn)
    yield conn
    conn.close()


def _svc(
    conn, *, now: int = 1_000_000, recipient: str | None = "+491"
) -> tuple[LimitService, RecordingSender]:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    sender = RecordingSender()
    svc = LimitService(
        repo=repo,
        sender=sender,
        default_recipient=recipient,
        clock=lambda: now,
    )
    return svc, sender


# --- record --------------------------------------------------------------


def test_record_persists_with_explicit_reset_at(conn) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn, now=1000)
    svc.record(
        "alpha",
        UsageLimitEvent(
            uuid="u1",
            timestamp="",
            reset_at="2024-01-01T00:00:00Z",
            limit_kind="session_5h",
        ),
    )
    repo = SqliteMaxLimitsRepository(conn)
    stored = repo.get(LimitKind.SESSION_5H)
    assert stored is not None
    assert stored.reset_at_ts == 1_704_067_200  # 2024-01-01T00:00:00Z


def test_record_defaults_when_reset_at_missing(conn) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn, now=10_000)
    svc.record(
        "alpha",
        UsageLimitEvent(uuid="u1", timestamp="", reset_at=None, limit_kind=None),
    )
    stored = SqliteMaxLimitsRepository(conn).get(LimitKind.SESSION_5H)
    assert stored is not None
    # Default window is 1 hour → reset is now+3600.
    assert stored.reset_at_ts == 10_000 + 3600


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("weekly", LimitKind.WEEKLY),
        ("week", LimitKind.WEEKLY),
        ("opus_sub", LimitKind.OPUS_SUB),
        ("opus", LimitKind.OPUS_SUB),
        ("session", LimitKind.SESSION_5H),
        ("something-made-up", LimitKind.SESSION_5H),  # default
        (None, LimitKind.SESSION_5H),
    ],
)
def test_record_maps_limit_kind(conn, raw, expected) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn, now=1000)
    svc.record(
        "alpha",
        UsageLimitEvent(
            uuid="u", timestamp="", reset_at="2024-01-01T00:00:00Z",
            limit_kind=raw,
        ),
    )
    assert (
        SqliteMaxLimitsRepository(conn).get(expected) is not None
    )


def test_record_preserves_warned_at_across_upserts(conn) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn, now=1000)
    # Seed a row with an existing warned_at marker.
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=500,
            warned_at_ts=400,
            remaining_pct=0.05,
        )
    )
    svc.record(
        "alpha",
        UsageLimitEvent(
            uuid="u", timestamp="",
            reset_at="2024-01-01T00:00:00Z",
            limit_kind="session_5h",
        ),
    )
    stored = repo.get(LimitKind.SESSION_5H)
    assert stored is not None
    assert stored.warned_at_ts == 400  # preserved
    assert stored.remaining_pct == 0.05  # preserved
    assert stored.reset_at_ts == 1_704_067_200  # refreshed


# --- check_guard --------------------------------------------------------


def test_check_guard_passes_when_no_active_limit(conn) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn, now=1000)
    svc.check_guard("alpha")  # does not raise


def test_check_guard_raises_on_active_limit(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=2000))
    svc, _sender = _svc(conn, now=1000)
    with pytest.raises(MaxLimitActiveError) as exc_info:
        svc.check_guard("alpha")
    assert exc_info.value.limit.kind == LimitKind.SESSION_5H


def test_check_guard_picks_shortest_active(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(MaxLimit(kind=LimitKind.WEEKLY, reset_at_ts=10_000))
    repo.upsert(MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=2_000))
    repo.upsert(MaxLimit(kind=LimitKind.OPUS_SUB, reset_at_ts=5_000))
    svc, _sender = _svc(conn, now=1000)
    with pytest.raises(MaxLimitActiveError) as exc_info:
        svc.check_guard("alpha")
    # Shortest remaining wins.
    assert exc_info.value.limit.kind == LimitKind.SESSION_5H


def test_check_guard_ignores_expired_limits(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=500))
    svc, _sender = _svc(conn, now=1000)
    svc.check_guard("alpha")  # expired → no raise


# --- maybe_warn ---------------------------------------------------------


def test_maybe_warn_fires_once_when_below_threshold(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=2000,
            remaining_pct=0.05,
        )
    )
    svc, sender = _svc(conn, now=1000)
    assert svc.maybe_warn() == 1
    assert len(sender.sent) == 1
    to, body = sender.sent[0]
    assert to == "+491"
    assert "⚠️" in body
    assert "session_5h" in body
    # Second call in the same window must be a no-op.
    assert svc.maybe_warn() == 0
    assert len(sender.sent) == 1


def test_maybe_warn_skips_when_above_threshold(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=2000,
            remaining_pct=0.50,
        )
    )
    svc, sender = _svc(conn, now=1000)
    assert svc.maybe_warn() == 0
    assert sender.sent == []


def test_maybe_warn_without_recipient_is_noop(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=2000,
            remaining_pct=0.05,
        )
    )
    svc, sender = _svc(conn, now=1000, recipient=None)
    assert svc.maybe_warn() == 0
    assert sender.sent == []


def test_maybe_warn_send_failure_does_not_mark_warned(conn) -> None:  # type: ignore[no-untyped-def]
    """A failed send shouldn't consume the warn-once budget — next
    tick we should still try again."""

    class FailingSender:
        def __init__(self) -> None:
            self.fail_next = True
            self.call_count = 0

        def send_text(self, *, to: str, body: str) -> None:
            self.call_count += 1
            if self.fail_next:
                self.fail_next = False
                raise RuntimeError("network down")

    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=2000,
            remaining_pct=0.05,
        )
    )
    sender = FailingSender()
    svc = LimitService(
        repo=repo,
        sender=sender,
        default_recipient="+491",
        clock=lambda: 1000,
    )
    assert svc.maybe_warn() == 0  # first call raises internally
    assert sender.call_count == 1
    # Second call succeeds.
    assert svc.maybe_warn() == 1
    assert sender.call_count == 2


# --- sweep_expired ------------------------------------------------------


def test_sweep_expired_removes_past_rows(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=500))
    repo.upsert(MaxLimit(kind=LimitKind.WEEKLY, reset_at_ts=2000))
    svc, _sender = _svc(conn, now=1000)
    assert svc.sweep_expired() == 1
    remaining = repo.list_all()
    assert [limit.kind for limit in remaining] == [LimitKind.WEEKLY]


def test_sweep_expired_empty_is_noop(conn) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn)
    assert svc.sweep_expired() == 0


# --- update_remaining --------------------------------------------------


def test_update_remaining_clamps_and_persists(conn) -> None:  # type: ignore[no-untyped-def]
    repo = SqliteMaxLimitsRepository(conn)
    repo.upsert(MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=2000))
    svc, _sender = _svc(conn, now=1000)
    svc.update_remaining(LimitKind.SESSION_5H, 1.5)  # out of range
    stored = repo.get(LimitKind.SESSION_5H)
    assert stored is not None
    assert stored.remaining_pct == 1.0  # clamped


def test_update_remaining_missing_row_is_noop(conn) -> None:  # type: ignore[no-untyped-def]
    svc, _sender = _svc(conn)
    svc.update_remaining(LimitKind.WEEKLY, 0.5)
    repo = SqliteMaxLimitsRepository(conn)
    assert repo.get(LimitKind.WEEKLY) is None
