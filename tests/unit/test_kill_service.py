"""Unit tests for whatsbot.application.kill_service (Phase 6 C6.1)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.application.kill_service import (
    KillOutcome,
    KillService,
    StopOutcome,
)
from whatsbot.application.lock_service import LockService
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    Mode,
    Project,
    SourceMode,
)
from whatsbot.ports.tmux_controller import TmuxError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@dataclass
class _FakeTmux:
    """Tracks every call so the tests can prove the right tmux ops fired."""

    _alive: set[str] = field(default_factory=set)
    has_session_calls: list[str] = field(default_factory=list)
    interrupt_calls: list[str] = field(default_factory=list)
    kill_session_calls: list[str] = field(default_factory=list)
    raise_on_interrupt: Exception | None = None
    raise_on_kill: Exception | None = None

    def has_session(self, name: str) -> bool:
        self.has_session_calls.append(name)
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:  # pragma: no cover
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:  # pragma: no cover
        del name, text

    def interrupt(self, name: str) -> None:
        if self.raise_on_interrupt is not None:
            raise self.raise_on_interrupt
        self.interrupt_calls.append(name)

    def kill_session(self, name: str) -> bool:
        if self.raise_on_kill is not None:
            raise self.raise_on_kill
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        del name, color, label


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


def _build(
    conn: sqlite3.Connection,
    *,
    alive: set[str] | None = None,
    with_lock: bool = False,
) -> tuple[KillService, _FakeTmux, SqliteSessionLockRepository, LockService]:
    tmux = _FakeTmux(_alive=set(alive or set()))
    lock_repo = SqliteSessionLockRepository(conn)
    locks = LockService(repo=lock_repo, clock=lambda: NOW)
    if with_lock:
        lock_repo.upsert(
            SessionLock(
                project_name="alpha",
                owner=LockOwner.BOT,
                acquired_at=NOW,
                last_activity_at=NOW,
            )
        )
    return KillService(tmux=tmux, lock_service=locks), tmux, lock_repo, locks


# ---- /stop -----------------------------------------------------------


def test_stop_sends_ctrl_c_when_session_alive(
    conn: sqlite3.Connection,
) -> None:
    svc, tmux, _, _ = _build(conn, alive={"wb-alpha"})
    outcome = svc.stop("alpha")
    assert outcome == StopOutcome(project_name="alpha", was_alive=True)
    assert tmux.interrupt_calls == ["wb-alpha"]
    # Session row stays alive — stop is a soft cancel.
    assert tmux.kill_session_calls == []


def test_stop_no_op_when_session_dead(
    conn: sqlite3.Connection,
) -> None:
    svc, tmux, _, _ = _build(conn, alive=set())
    outcome = svc.stop("alpha")
    assert outcome.was_alive is False
    assert tmux.interrupt_calls == []


def test_stop_validates_project_name(conn: sqlite3.Connection) -> None:
    svc, _, _, _ = _build(conn)
    with pytest.raises(InvalidProjectNameError):
        svc.stop("BAD NAME")


def test_stop_propagates_tmux_error(conn: sqlite3.Connection) -> None:
    """A real tmux error should bubble up so the command handler can
    surface it — silent swallow would hide a broken /stop."""
    svc, tmux, _, _ = _build(conn, alive={"wb-alpha"})
    tmux.raise_on_interrupt = TmuxError("tmux gone")
    with pytest.raises(TmuxError):
        svc.stop("alpha")


# ---- /kill -----------------------------------------------------------


def test_kill_destroys_session_and_releases_lock(
    conn: sqlite3.Connection,
) -> None:
    svc, tmux, lock_repo, _ = _build(
        conn, alive={"wb-alpha"}, with_lock=True
    )
    outcome = svc.kill("alpha")
    assert outcome == KillOutcome(
        project_name="alpha", was_alive=True, lock_released=True
    )
    assert tmux.kill_session_calls == ["wb-alpha"]
    # Lock row gone.
    assert lock_repo.get("alpha") is None


def test_kill_reports_no_session_but_still_releases_lock(
    conn: sqlite3.Connection,
) -> None:
    """Stale lock + dead tmux is the post-crash recovery shape — /kill
    should still clear the lock so the user isn't stuck."""
    svc, tmux, lock_repo, _ = _build(
        conn, alive=set(), with_lock=True
    )
    outcome = svc.kill("alpha")
    assert outcome.was_alive is False
    assert outcome.lock_released is True
    assert tmux.kill_session_calls == ["wb-alpha"]
    assert lock_repo.get("alpha") is None


def test_kill_without_lock_service(conn: sqlite3.Connection) -> None:
    """Older test paths can construct a KillService without a
    LockService — the kill must still work, lock_released=False."""
    tmux = _FakeTmux(_alive={"wb-alpha"})
    svc = KillService(tmux=tmux, lock_service=None)
    outcome = svc.kill("alpha")
    assert outcome.was_alive is True
    assert outcome.lock_released is False


def test_kill_swallows_lock_release_failure(
    conn: sqlite3.Connection,
) -> None:
    """If the lock release blows up (DB locked, e.g.), the tmux kill
    has already happened — we must not raise on top, just log."""
    svc, tmux, _, locks = _build(conn, alive={"wb-alpha"}, with_lock=True)

    def boom(_p: str) -> bool:
        raise RuntimeError("lock release failed")

    locks.release = boom  # type: ignore[method-assign]
    outcome = svc.kill("alpha")
    assert outcome.was_alive is True
    assert outcome.lock_released is False
    # Pane was killed despite the lock failure.
    assert tmux.kill_session_calls == ["wb-alpha"]


def test_kill_validates_project_name(conn: sqlite3.Connection) -> None:
    svc, _, _, _ = _build(conn)
    with pytest.raises(InvalidProjectNameError):
        svc.kill("BAD NAME")
