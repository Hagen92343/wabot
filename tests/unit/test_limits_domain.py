"""C8.1 — domain/limits.py pure tests."""

from __future__ import annotations

import pytest

from whatsbot.domain.limits import (
    LOW_REMAINING_THRESHOLD,
    LimitKind,
    MaxLimit,
    format_reset_duration,
    is_active,
    parse_reset_at,
    shortest_active,
    should_warn,
)

# --- is_active / shortest_active -----------------------------------------


def test_is_active_before_reset() -> None:
    lim = MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=200)
    assert is_active(lim, now=100)


def test_is_active_after_reset() -> None:
    lim = MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=200)
    assert not is_active(lim, now=200)
    assert not is_active(lim, now=201)


def test_shortest_active_empty() -> None:
    assert shortest_active([], now=100) is None


def test_shortest_active_all_expired() -> None:
    lims = [
        MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=50),
        MaxLimit(kind=LimitKind.WEEKLY, reset_at_ts=80),
    ]
    assert shortest_active(lims, now=100) is None


def test_shortest_active_returns_earliest_reset() -> None:
    earliest = MaxLimit(kind=LimitKind.SESSION_5H, reset_at_ts=150)
    later = MaxLimit(kind=LimitKind.WEEKLY, reset_at_ts=300)
    also_active = MaxLimit(kind=LimitKind.OPUS_SUB, reset_at_ts=200)
    out = shortest_active([later, earliest, also_active], now=100)
    assert out is earliest


# --- format_reset_duration -----------------------------------------------


@pytest.mark.parametrize(
    ("delta", "expected"),
    [
        (0, "<1s"),
        (-5, "<1s"),
        (15, "15s"),
        (60, "1m"),
        (119, "1m"),  # 1m 59s collapses to 1m when <1h
        (3_660, "1h 1m"),
        (3 * 3600 + 22 * 60, "3h 22m"),
        (7 * 24 * 3600 + 30 * 60, f"{7 * 24}h 30m"),
    ],
)
def test_format_reset_duration(delta: int, expected: str) -> None:
    assert format_reset_duration(1000 + delta, now=1000) == expected


# --- should_warn ---------------------------------------------------------


def test_should_warn_unknown_remaining_does_not_warn() -> None:
    lim = MaxLimit(
        kind=LimitKind.SESSION_5H, reset_at_ts=200, remaining_pct=-1.0
    )
    assert not should_warn(lim, now=100)


def test_should_warn_above_threshold_does_not_warn() -> None:
    lim = MaxLimit(
        kind=LimitKind.SESSION_5H, reset_at_ts=200, remaining_pct=0.25
    )
    assert not should_warn(lim, now=100)


def test_should_warn_expired_does_not_warn() -> None:
    lim = MaxLimit(
        kind=LimitKind.SESSION_5H, reset_at_ts=200, remaining_pct=0.05
    )
    assert not should_warn(lim, now=250)


def test_should_warn_never_warned_fires() -> None:
    lim = MaxLimit(
        kind=LimitKind.SESSION_5H,
        reset_at_ts=200,
        remaining_pct=LOW_REMAINING_THRESHOLD - 0.01,
        warned_at_ts=None,
    )
    assert should_warn(lim, now=100)


def test_should_warn_already_warned_in_current_window_skips() -> None:
    # Fresh warning inside the "same window" floor (=reset-48h).
    lim = MaxLimit(
        kind=LimitKind.SESSION_5H,
        reset_at_ts=200,
        remaining_pct=0.05,
        warned_at_ts=199,
    )
    assert not should_warn(lim, now=100)


def test_should_warn_old_warning_triggers_again_after_rotation() -> None:
    # Warned a long time ago — new window → warn again.
    lim = MaxLimit(
        kind=LimitKind.SESSION_5H,
        reset_at_ts=200 + 7 * 24 * 3600,  # weekly-size later
        remaining_pct=0.05,
        warned_at_ts=50,  # ancient
    )
    assert should_warn(lim, now=190 + 7 * 24 * 3600)


# --- parse_reset_at ------------------------------------------------------


def test_parse_reset_at_none() -> None:
    assert parse_reset_at(None) is None


def test_parse_reset_at_int() -> None:
    assert parse_reset_at(1_700_000_000) == 1_700_000_000


def test_parse_reset_at_float_truncates() -> None:
    assert parse_reset_at(1_700_000_000.75) == 1_700_000_000


def test_parse_reset_at_bool_rejected() -> None:
    # bool is a subclass of int — must be explicitly rejected.
    assert parse_reset_at(True) is None


def test_parse_reset_at_iso_z_suffix() -> None:
    # 2024-01-01T00:00:00Z == 1_704_067_200
    assert parse_reset_at("2024-01-01T00:00:00Z") == 1_704_067_200


def test_parse_reset_at_iso_offset() -> None:
    assert parse_reset_at("2024-01-01T01:00:00+01:00") == 1_704_067_200


def test_parse_reset_at_iso_naive_assumes_utc() -> None:
    assert parse_reset_at("2024-01-01T00:00:00") == 1_704_067_200


def test_parse_reset_at_iso_fractional_seconds() -> None:
    assert parse_reset_at("2024-01-01T00:00:00.500Z") == 1_704_067_200


def test_parse_reset_at_invalid_returns_none() -> None:
    assert parse_reset_at("not-a-date") is None
    assert parse_reset_at("") is None
    assert parse_reset_at("   ") is None
    assert parse_reset_at([1, 2, 3]) is None
