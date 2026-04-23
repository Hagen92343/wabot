"""Pure-domain tests for ``whatsbot.domain.lockdown`` (Phase 6 C6.2)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from whatsbot.domain.lockdown import (
    ALL_LOCKDOWN_REASONS,
    LOCKDOWN_REASON_PANIC,
    LOCKDOWN_REASON_WATCHDOG,
    LockdownState,
    disengaged,
    engage,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


def test_disengaged_is_clean_state() -> None:
    state = disengaged()
    assert state.engaged is False
    assert state.engaged_at is None
    assert state.reason is None
    assert state.engaged_by is None


def test_engage_from_disengaged() -> None:
    state = engage(
        disengaged(),
        now=NOW,
        reason=LOCKDOWN_REASON_PANIC,
        engaged_by="panic",
    )
    assert state == LockdownState(
        engaged=True,
        engaged_at=NOW,
        reason=LOCKDOWN_REASON_PANIC,
        engaged_by="panic",
    )


def test_engage_is_idempotent_preserves_first_trigger() -> None:
    """Forensics needs to see the *first* trigger, not the latest."""
    first = engage(
        disengaged(),
        now=NOW,
        reason=LOCKDOWN_REASON_PANIC,
        engaged_by="panic",
    )
    later = datetime(2026, 4, 23, 13, 0, tzinfo=UTC)
    second = engage(
        first,
        now=later,
        reason=LOCKDOWN_REASON_WATCHDOG,
        engaged_by="watchdog",
    )
    assert second is first  # same dataclass instance, no copy


def test_engage_rejects_unknown_reason() -> None:
    with pytest.raises(ValueError, match="Unknown lockdown reason"):
        engage(
            disengaged(),
            now=NOW,
            reason="hacker",
        )


def test_all_known_reasons_are_accepted() -> None:
    for reason in ALL_LOCKDOWN_REASONS:
        state = engage(disengaged(), now=NOW, reason=reason)
        assert state.reason == reason
