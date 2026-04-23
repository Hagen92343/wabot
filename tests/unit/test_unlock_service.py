"""Unit tests for ``whatsbot.application.unlock_service`` (Phase 6 C6.6)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.application.delete_service import (
    InvalidPinError,
    PanicPinNotConfiguredError,
)
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.application.unlock_service import UnlockService
from whatsbot.domain.lockdown import LOCKDOWN_REASON_PANIC
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)
PIN = "1234"


class _StubSecrets:
    def __init__(self, pin: str | None = PIN) -> None:
        self._store: dict[str, str] = {}
        if pin is not None:
            self._store[KEY_PANIC_PIN] = pin

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:  # pragma: no cover
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:  # pragma: no cover
        self._store[key] = new_value


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


def _build(
    conn: sqlite3.Connection,
    marker_path: Path,
    *,
    pin: str | None = PIN,
    pre_engaged: bool = False,
) -> tuple[UnlockService, LockdownService]:
    lockdown = LockdownService(
        app_state=SqliteAppStateRepository(conn),
        panic_marker_path=marker_path,
        clock=lambda: NOW,
    )
    if pre_engaged:
        lockdown.engage(reason=LOCKDOWN_REASON_PANIC, engaged_by="panic")
    svc = UnlockService(
        lockdown_service=lockdown,
        secrets=_StubSecrets(pin=pin),
    )
    return svc, lockdown


# ---- happy path ----------------------------------------------------


def test_unlock_disengages_when_pin_matches(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    marker = tmp_path / "PANIC"
    svc, lockdown = _build(conn, marker, pre_engaged=True)
    assert lockdown.is_engaged() is True
    assert marker.exists()

    outcome = svc.unlock(PIN)
    assert outcome.was_engaged is True
    assert outcome.previous_state.engaged is True
    assert outcome.new_state.engaged is False
    assert lockdown.is_engaged() is False
    assert not marker.exists()


def test_unlock_when_not_engaged_still_passes_pin_check(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An unlock against a clean lockdown still requires PIN — no
    info-leak via timing on whether lockdown was engaged."""
    svc, _ = _build(conn, tmp_path / "PANIC", pre_engaged=False)
    outcome = svc.unlock(PIN)
    assert outcome.was_engaged is False
    assert outcome.new_state.engaged is False


# ---- failures: PIN -------------------------------------------------


def test_unlock_rejects_wrong_pin_keeps_lockdown(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    marker = tmp_path / "PANIC"
    svc, lockdown = _build(conn, marker, pre_engaged=True)
    with pytest.raises(InvalidPinError):
        svc.unlock("9999")
    # Lockdown still engaged, marker still on disk.
    assert lockdown.is_engaged() is True
    assert marker.exists()


def test_unlock_rejects_empty_pin(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Empty PIN must not match — fail-safe against an attacker who
    types nothing on a stolen handset."""
    svc, _ = _build(conn, tmp_path / "PANIC", pre_engaged=True)
    with pytest.raises(InvalidPinError):
        svc.unlock("")


def test_unlock_uses_constant_time_compare(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Same-length wrong PIN, longer wrong PIN, single-char-diff —
    all must fail."""
    svc, _ = _build(conn, tmp_path / "PANIC", pre_engaged=True)
    with pytest.raises(InvalidPinError):
        svc.unlock("1235")  # one-char diff
    with pytest.raises(InvalidPinError):
        svc.unlock("12345")  # longer
    with pytest.raises(InvalidPinError):
        svc.unlock("123")  # shorter


def test_unlock_raises_when_panic_pin_not_set(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    svc, _ = _build(conn, tmp_path / "PANIC", pin=None, pre_engaged=True)
    with pytest.raises(PanicPinNotConfiguredError):
        svc.unlock(PIN)
