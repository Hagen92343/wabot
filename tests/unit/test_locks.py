"""Unit tests for whatsbot.domain.locks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from whatsbot.domain.locks import (
    LOCK_TIMEOUT_SECONDS,
    AcquireOutcome,
    LockOwner,
    SessionLock,
    evaluate_bot_attempt,
    is_expired,
    mark_local_input,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


def _lock(
    owner: LockOwner,
    *,
    last_activity_at: datetime | None = None,
    acquired_at: datetime | None = None,
) -> SessionLock:
    last = last_activity_at or NOW
    return SessionLock(
        project_name="alpha",
        owner=owner,
        acquired_at=acquired_at or last,
        last_activity_at=last,
    )


# ---- evaluate_bot_attempt ----------------------------------------------


def test_bot_attempt_on_none_grants() -> None:
    outcome, new = evaluate_bot_attempt(None, now=NOW, project_name="alpha")
    assert outcome is AcquireOutcome.GRANTED
    assert new.owner is LockOwner.BOT
    assert new.project_name == "alpha"


def test_bot_attempt_on_free_grants() -> None:
    outcome, new = evaluate_bot_attempt(
        _lock(LockOwner.FREE), now=NOW, project_name="alpha"
    )
    assert outcome is AcquireOutcome.GRANTED
    assert new.owner is LockOwner.BOT


def test_bot_attempt_on_own_bot_refreshes_activity() -> None:
    earlier = NOW - timedelta(seconds=30)
    current = _lock(
        LockOwner.BOT, last_activity_at=earlier, acquired_at=earlier
    )
    outcome, new = evaluate_bot_attempt(
        current, now=NOW, project_name="alpha"
    )
    assert outcome is AcquireOutcome.GRANTED
    assert new.owner is LockOwner.BOT
    # acquired_at stays put; last_activity_at advances to NOW.
    assert new.acquired_at == earlier
    assert new.last_activity_at == NOW


def test_bot_attempt_on_active_local_denies() -> None:
    recent = NOW - timedelta(seconds=10)
    current = _lock(LockOwner.LOCAL, last_activity_at=recent)
    outcome, new = evaluate_bot_attempt(
        current, now=NOW, project_name="alpha"
    )
    assert outcome is AcquireOutcome.DENIED_LOCAL_HELD
    # The returned lock is unchanged.
    assert new is current


def test_bot_attempt_on_idle_local_auto_releases() -> None:
    stale = NOW - timedelta(seconds=LOCK_TIMEOUT_SECONDS + 5)
    current = _lock(LockOwner.LOCAL, last_activity_at=stale)
    outcome, new = evaluate_bot_attempt(
        current, now=NOW, project_name="alpha"
    )
    assert outcome is AcquireOutcome.AUTO_RELEASED_THEN_GRANTED
    assert new.owner is LockOwner.BOT
    assert new.acquired_at == NOW
    assert new.last_activity_at == NOW


def test_bot_attempt_custom_timeout_honoured() -> None:
    # Lock is 5s idle; a 3s timeout auto-releases, a 10s one doesn't.
    recent = NOW - timedelta(seconds=5)
    current = _lock(LockOwner.LOCAL, last_activity_at=recent)

    strict_outcome, _ = evaluate_bot_attempt(
        current, now=NOW, timeout_seconds=3, project_name="alpha"
    )
    assert strict_outcome is AcquireOutcome.AUTO_RELEASED_THEN_GRANTED

    relaxed_outcome, _ = evaluate_bot_attempt(
        current, now=NOW, timeout_seconds=10, project_name="alpha"
    )
    assert relaxed_outcome is AcquireOutcome.DENIED_LOCAL_HELD


# ---- mark_local_input --------------------------------------------------


def test_mark_local_input_from_none_opens_local_lock() -> None:
    new = mark_local_input(None, now=NOW, project_name="alpha")
    assert new.owner is LockOwner.LOCAL
    assert new.acquired_at == NOW
    assert new.last_activity_at == NOW


def test_mark_local_input_from_bot_preempts() -> None:
    current = _lock(LockOwner.BOT)
    new = mark_local_input(current, now=NOW, project_name="alpha")
    assert new.owner is LockOwner.LOCAL
    # Fresh acquired_at since ownership transferred.
    assert new.acquired_at == NOW


def test_mark_local_input_from_free_takes_lock() -> None:
    current = _lock(LockOwner.FREE)
    new = mark_local_input(current, now=NOW, project_name="alpha")
    assert new.owner is LockOwner.LOCAL
    assert new.acquired_at == NOW


def test_mark_local_input_from_local_refreshes_activity_only() -> None:
    old = NOW - timedelta(seconds=30)
    current = _lock(LockOwner.LOCAL, last_activity_at=old, acquired_at=old)
    new = mark_local_input(current, now=NOW, project_name="alpha")
    assert new.owner is LockOwner.LOCAL
    assert new.acquired_at == old  # unchanged
    assert new.last_activity_at == NOW


# ---- is_expired --------------------------------------------------------


def test_is_expired_only_for_stale_local_locks() -> None:
    stale = NOW - timedelta(seconds=LOCK_TIMEOUT_SECONDS + 1)
    assert is_expired(
        _lock(LockOwner.LOCAL, last_activity_at=stale), now=NOW
    )


def test_is_expired_false_for_active_local() -> None:
    recent = NOW - timedelta(seconds=5)
    assert not is_expired(
        _lock(LockOwner.LOCAL, last_activity_at=recent), now=NOW
    )


def test_is_expired_false_for_bot_owned_regardless_of_idle() -> None:
    stale = NOW - timedelta(seconds=3600)
    # A stale bot lock is NOT auto-released — only local can be
    # pre-empted by timeout. Bot keeps what it has until /release.
    assert not is_expired(
        _lock(LockOwner.BOT, last_activity_at=stale), now=NOW
    )


def test_is_expired_false_for_none_and_free() -> None:
    assert not is_expired(None, now=NOW)
    assert not is_expired(_lock(LockOwner.FREE), now=NOW)
