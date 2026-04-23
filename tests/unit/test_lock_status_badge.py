"""Phase 5 C5.5 — tmux status-bar lock-owner badge.

Three layers:

1. Pure ``lock_owner_badge`` helper (domain).
2. ``SessionService._paint_status_bar`` actually appends the owner
   badge to the label it ships to ``tmux.set_status``.
3. ``LockService`` fires ``on_owner_change`` on every owner-flipping
   operation (acquire, force, note_local_input, release, sweep), but
   *not* on no-op refreshes (bot re-acquires, repeated local input).
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.application.lock_service import LockService
from whatsbot.application.session_service import SessionService
from whatsbot.domain.locks import (
    LockOwner,
    SessionLock,
    lock_owner_badge,
)
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


# --- 1. Pure helper -------------------------------------------------


def test_lock_owner_badge_bot() -> None:
    assert lock_owner_badge(LockOwner.BOT) == "🤖 BOT"


def test_lock_owner_badge_local() -> None:
    assert lock_owner_badge(LockOwner.LOCAL) == "👤 LOCAL"


def test_lock_owner_badge_free() -> None:
    assert lock_owner_badge(LockOwner.FREE) == "— FREE"


def test_lock_owner_badge_none_renders_as_free() -> None:
    """A missing row in ``session_locks`` means nothing-holds-it. The
    bar should still tell the user *something* — render FREE."""
    assert lock_owner_badge(None) == "— FREE"


# --- 2. SessionService paints the badge -----------------------------


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    set_status_calls: list[tuple[str, str, str]] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        del name, text

    def interrupt(self, name: str) -> None:
        del name

    def kill_session(self, name: str) -> bool:
        self._alive.discard(name)
        return True

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        self.set_status_calls.append((name, color, label))


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
    SqliteClaudeSessionRepository(c).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-alpha",
            transcript_path="",
            started_at=NOW,
            current_mode=Mode.NORMAL,
        )
    )
    try:
        yield c
    finally:
        c.close()


def _build_session_service(
    conn: sqlite3.Connection, tmux: _FakeTmux, projects_root: Path
) -> tuple[SessionService, LockService, SqliteSessionLockRepository]:
    lock_repo = SqliteSessionLockRepository(conn)
    locks = LockService(repo=lock_repo, clock=lambda: NOW)
    svc = SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=projects_root,
        lock_service=locks,
    )
    return svc, locks, lock_repo


def test_paint_status_bar_includes_free_badge_for_no_lock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    tmux = _FakeTmux()
    svc, _, _ = _build_session_service(conn, tmux, tmp_path)
    svc.ensure_started("alpha")

    # ensure_started paints once. The label must include the owner badge.
    assert tmux.set_status_calls, "expected at least one set_status call"
    _, _, label = tmux.set_status_calls[-1]
    assert "🟢 NORMAL" in label
    assert "— FREE" in label
    assert "wb-alpha" in label


def test_paint_status_bar_includes_local_badge(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    tmux = _FakeTmux()
    svc, _, lock_repo = _build_session_service(conn, tmux, tmp_path)
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW,
            last_activity_at=NOW,
        )
    )
    svc.ensure_started("alpha")
    _, _, label = tmux.set_status_calls[-1]
    assert "👤 LOCAL" in label


def test_paint_status_bar_includes_bot_badge(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    tmux = _FakeTmux()
    svc, _, lock_repo = _build_session_service(conn, tmux, tmp_path)
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.BOT,
            acquired_at=NOW,
            last_activity_at=NOW,
        )
    )
    svc.ensure_started("alpha")
    _, _, label = tmux.set_status_calls[-1]
    assert "🤖 BOT" in label


def test_repaint_status_bar_no_op_when_tmux_dead(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Repaint must not crash when the tmux session has gone away —
    the LockService sweeper hits this on bot startup before any
    project has its tmux up."""
    tmux = _FakeTmux()
    svc, _, _ = _build_session_service(conn, tmux, tmp_path)
    # tmux is empty — repaint should silently no-op.
    svc.repaint_status_bar("alpha")
    assert tmux.set_status_calls == []


def test_repaint_status_bar_swallows_missing_project(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """An unknown project shouldn't crash the cosmetic repaint —
    the lock could outlive the project briefly during /rm cascades."""
    tmux = _FakeTmux()
    svc, _, _ = _build_session_service(conn, tmux, tmp_path)
    # Pretend tmux has it (so we don't hit the early no-op),
    # but don't seed the project row.
    tmux._alive.add("wb-ghost")  # type: ignore[attr-defined]
    svc.repaint_status_bar("ghost")
    # No set_status call landed — we returned without painting.
    assert all("ghost" not in name for name, _, _ in tmux.set_status_calls)


# --- 3. LockService fires on_owner_change ---------------------------


def test_acquire_for_bot_fires_callback_on_first_grant(
    conn: sqlite3.Connection,
) -> None:
    calls: list[str] = []
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    svc.acquire_for_bot("alpha")
    assert calls == ["alpha"]


def test_acquire_for_bot_no_callback_on_re_acquire(
    conn: sqlite3.Connection,
) -> None:
    """Bot already holds the lock → re-acquire just refreshes
    activity. Owner badge would render the same → no repaint
    pressure."""
    calls: list[str] = []
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    svc.acquire_for_bot("alpha")
    svc.acquire_for_bot("alpha")
    assert calls == ["alpha"]  # exactly one fire


def test_force_bot_fires_callback_when_flipping_from_local(
    conn: sqlite3.Connection,
) -> None:
    calls: list[str] = []
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    SqliteSessionLockRepository(conn).upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW,
            last_activity_at=NOW,
        )
    )
    svc.force_bot("alpha")
    assert calls == ["alpha"]


def test_note_local_input_fires_callback_only_on_first_local(
    conn: sqlite3.Connection,
) -> None:
    """Repeated local-input pulses (every keystroke!) on an
    already-local lock would otherwise spam the repaint path."""
    calls: list[str] = []
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    svc.note_local_input("alpha")
    svc.note_local_input("alpha")
    svc.note_local_input("alpha")
    assert calls == ["alpha"]


def test_release_fires_callback_when_row_existed(
    conn: sqlite3.Connection,
) -> None:
    calls: list[str] = []
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    svc.acquire_for_bot("alpha")
    calls.clear()
    svc.release("alpha")
    assert calls == ["alpha"]


def test_release_no_callback_when_nothing_to_release(
    conn: sqlite3.Connection,
) -> None:
    calls: list[str] = []
    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    removed = svc.release("alpha")
    assert removed is False
    assert calls == []


def test_sweep_expired_fires_callback_per_reaped_project(
    conn: sqlite3.Connection,
) -> None:
    project_repo = SqliteProjectRepository(conn)
    project_repo.create(
        Project(
            name="beta",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.NORMAL,
        )
    )
    lock_repo = SqliteSessionLockRepository(conn)
    long_ago = NOW - timedelta(minutes=5)
    for name in ("alpha", "beta"):
        lock_repo.upsert(
            SessionLock(
                project_name=name,
                owner=LockOwner.LOCAL,
                acquired_at=long_ago,
                last_activity_at=long_ago,
            )
        )

    calls: list[str] = []
    svc = LockService(
        repo=lock_repo,
        clock=lambda: NOW,
        on_owner_change=calls.append,
    )
    reaped = svc.sweep_expired()
    assert sorted(reaped) == ["alpha", "beta"]
    assert sorted(calls) == ["alpha", "beta"]


def test_callback_failures_dont_break_lock_op(
    conn: sqlite3.Connection,
) -> None:
    """The callback is purely cosmetic. If it explodes, the lock
    must still take effect."""
    def boom(_p: str) -> None:
        raise RuntimeError("intentional")

    svc = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
        on_owner_change=boom,
    )
    # Must not propagate.
    svc.acquire_for_bot("alpha")
    persisted = SqliteSessionLockRepository(conn).get("alpha")
    assert persisted is not None
    assert persisted.owner is LockOwner.BOT
