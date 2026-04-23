"""Phase 6 C6.6 — StartupRecovery respects lockdown.

When the bot restarts and lockdown is engaged (panic touch-file
on disk, app_state row engaged), recovery skips both YOLO reset
and session restore. The bot stays up so it can answer /unlock,
but it doesn't relaunch any Claude — Spec §7.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.application.session_service import SessionService
from whatsbot.application.startup_recovery import StartupRecovery
from whatsbot.domain.lockdown import LOCKDOWN_REASON_PANIC
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    new_session_calls: list[str] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:
        del cwd
        self.new_session_calls.append(name)
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:  # pragma: no cover
        del name, text

    def interrupt(self, name: str) -> None:  # pragma: no cover
        del name

    def kill_session(self, name: str) -> bool:  # pragma: no cover
        self._alive.discard(name)
        return True

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:  # pragma: no cover
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:  # pragma: no cover
        del name, color, label


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    project_repo = SqliteProjectRepository(c)
    project_repo.create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.YOLO,
        )
    )
    SqliteClaudeSessionRepository(c).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-alpha",
            transcript_path="",
            started_at=NOW,
            current_mode=Mode.YOLO,
        )
    )
    try:
        yield c
    finally:
        c.close()


def _build(
    conn: sqlite3.Connection, tmp_path: Path, *, engaged: bool
) -> tuple[StartupRecovery, _FakeTmux, LockdownService]:
    tmux = _FakeTmux()
    session_service = SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=tmp_path,
    )
    lockdown = LockdownService(
        app_state=SqliteAppStateRepository(conn),
        panic_marker_path=tmp_path / "PANIC",
        clock=lambda: NOW,
    )
    if engaged:
        lockdown.engage(reason=LOCKDOWN_REASON_PANIC, engaged_by="panic")
    recovery = StartupRecovery(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        mode_event_repo=SqliteModeEventRepository(conn),
        session_service=session_service,
        lockdown_service=lockdown,
    )
    return recovery, tmux, lockdown


# ---- lockdown blocks recovery ----------------------------------


def test_recovery_skips_when_lockdown_engaged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    recovery, tmux, _ = _build(conn, tmp_path, engaged=True)
    report = recovery.run()
    assert report.skipped_for_lockdown is True
    assert report.yolo_resets == ()
    assert report.restored_sessions == ()
    # No tmux session should have been created.
    assert tmux.new_session_calls == []
    # Project still YOLO — *not* coerced to Normal.
    project = SqliteProjectRepository(conn).get("alpha")
    assert project.mode is Mode.YOLO


def test_recovery_runs_normally_when_lockdown_clear(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    recovery, tmux, _ = _build(conn, tmp_path, engaged=False)
    report = recovery.run()
    assert report.skipped_for_lockdown is False
    assert "alpha" in report.yolo_resets
    # Session restoration kicked off the tmux launch.
    assert tmux.new_session_calls == ["wb-alpha"]
    # Project mode coerced to Normal.
    project = SqliteProjectRepository(conn).get("alpha")
    assert project.mode is Mode.NORMAL


def test_recovery_without_lockdown_service_runs_normally(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Phase-4 wiring path that doesn't pass a LockdownService —
    must still work (backwards-compat)."""
    tmux = _FakeTmux()
    session_service = SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=tmp_path,
    )
    recovery = StartupRecovery(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        mode_event_repo=SqliteModeEventRepository(conn),
        session_service=session_service,
        # lockdown_service omitted — old call path.
    )
    report = recovery.run()
    assert report.skipped_for_lockdown is False
    assert "alpha" in report.yolo_resets
