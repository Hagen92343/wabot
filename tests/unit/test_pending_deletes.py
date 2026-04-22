"""Unit tests for whatsbot.domain.pending_deletes (pure logic)."""

from __future__ import annotations

import pytest

from whatsbot.domain.pending_deletes import (
    CONFIRM_WINDOW_SECONDS,
    PendingDelete,
    compute_deadline,
)

pytestmark = pytest.mark.unit


def test_confirm_window_is_sixty_seconds() -> None:
    # Spec §11 says /rm opens a 60-second window. Any change here must be
    # deliberate — the command-handler copy uses the same constant.
    assert CONFIRM_WINDOW_SECONDS == 60


def test_compute_deadline_adds_window() -> None:
    assert compute_deadline(now_ts=1_000) == 1_060
    assert compute_deadline(now_ts=1_000, window=5) == 1_005


def test_compute_deadline_rejects_negative_window() -> None:
    with pytest.raises(ValueError):
        compute_deadline(now_ts=1_000, window=-1)


def test_is_expired_before_and_after_deadline() -> None:
    pending = PendingDelete(project_name="alpha", deadline_ts=1_060)
    assert pending.is_expired(1_059) is False
    assert pending.is_expired(1_060) is True  # inclusive
    assert pending.is_expired(1_100) is True


def test_seconds_left_positive_and_clamped() -> None:
    pending = PendingDelete(project_name="alpha", deadline_ts=1_060)
    assert pending.seconds_left(1_000) == 60
    assert pending.seconds_left(1_059) == 1
    assert pending.seconds_left(1_060) == 0  # clamped at boundary
    assert pending.seconds_left(9_999) == 0  # clamped past expiry
