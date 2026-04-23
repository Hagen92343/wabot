"""Unit tests for whatsbot.application.startup_recovery."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.session_service import SessionService
from whatsbot.application.startup_recovery import StartupRecovery
from whatsbot.domain.mode_events import ModeEventKind
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession

pytestmark = pytest.mark.unit


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    new_session_calls: list[tuple[str, str]] = field(default_factory=list)
    send_text_calls: list[tuple[str, str]] = field(default_factory=list)
    set_status_calls: list[tuple[str, str, str]] = field(default_factory=list)
    kill_session_calls: list[str] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:
        del cwd
        self.new_session_calls.append((name, ""))
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        self.send_text_calls.append((name, text))

    def kill_session(self, name: str) -> bool:
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        self.set_status_calls.append((name, color, label))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


def _seed_project(
    conn: sqlite3.Connection,
    name: str,
    mode: Mode,
    *,
    with_session: bool = True,
    session_id: str | None = None,
) -> None:
    SqliteProjectRepository(conn).create(
        Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=mode,
        )
    )
    if with_session:
        # session_id must be unique across rows (schema constraint) —
        # default it per project so tests with multiple projects
        # don't collide on the default.
        resolved_session_id = session_id if session_id is not None else f"sess-{name}"
        SqliteClaudeSessionRepository(conn).upsert(
            ClaudeSession(
                project_name=name,
                session_id=resolved_session_id,
                transcript_path="",
                started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
                current_mode=mode,
            )
        )


def _build_recovery(
    conn: sqlite3.Connection,
    projects_root: Path,
    tmux: _FakeTmux,
) -> StartupRecovery:
    session_service = SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=projects_root,
    )
    return StartupRecovery(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        mode_event_repo=SqliteModeEventRepository(conn),
        session_service=session_service,
    )


# ---- reset_yolo_to_normal ---------------------------------------------


def test_reset_yolo_to_normal_flips_only_yolo_projects(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.YOLO)
    _seed_project(conn, "beta", Mode.STRICT)
    _seed_project(conn, "gamma", Mode.YOLO)
    recovery = _build_recovery(conn, projects_root, _FakeTmux())

    resets = recovery.reset_yolo_to_normal()

    assert set(resets) == {"alpha", "gamma"}
    project_repo = SqliteProjectRepository(conn)
    assert project_repo.get("alpha").mode is Mode.NORMAL
    assert project_repo.get("beta").mode is Mode.STRICT
    assert project_repo.get("gamma").mode is Mode.NORMAL


def test_reset_yolo_writes_reboot_reset_mode_events(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.YOLO)
    recovery = _build_recovery(conn, projects_root, _FakeTmux())

    recovery.reset_yolo_to_normal()

    events = SqliteModeEventRepository(conn).list_for_project("alpha")
    assert len(events) == 1
    ev = events[0]
    assert ev.kind is ModeEventKind.REBOOT_RESET
    assert ev.from_mode is Mode.YOLO
    assert ev.to_mode is Mode.NORMAL


def test_reset_yolo_is_noop_when_no_yolo_projects(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.NORMAL)
    recovery = _build_recovery(conn, projects_root, _FakeTmux())

    resets = recovery.reset_yolo_to_normal()

    assert resets == ()
    assert SqliteModeEventRepository(conn).list_for_project("alpha") == []


# ---- restore_sessions ------------------------------------------------


def test_restore_sessions_calls_ensure_started_for_each_row(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.NORMAL)
    _seed_project(conn, "beta", Mode.STRICT)
    tmux = _FakeTmux()
    recovery = _build_recovery(conn, projects_root, tmux)

    restored, failed = recovery.restore_sessions()

    assert set(restored) == {"alpha", "beta"}
    assert failed == ()
    # Two tmux sessions were created — one per project.
    assert sorted(name for name, _ in tmux.new_session_calls) == [
        "wb-alpha",
        "wb-beta",
    ]


def test_restore_sessions_preserves_session_id_via_resume(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.NORMAL, session_id="sess-alpha-42")
    tmux = _FakeTmux()
    recovery = _build_recovery(conn, projects_root, tmux)

    recovery.restore_sessions()

    # The launch command carries --resume <session_id> so context
    # is preserved across the restart.
    launches = [text for name, text in tmux.send_text_calls if name == "wb-alpha"]
    assert any("--resume sess-alpha-42" in text for text in launches)


def test_restore_sessions_continues_after_single_failure(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.NORMAL)
    _seed_project(conn, "beta", Mode.NORMAL)

    # Force ensure_started to fail for 'alpha' only; 'beta' should
    # still restore. We do this by monkeypatching the SessionService
    # inside the recovery.
    tmux = _FakeTmux()
    recovery = _build_recovery(conn, projects_root, tmux)
    real_ensure = recovery._session_service.ensure_started

    def broken(project_name: str) -> object:
        if project_name == "alpha":
            raise RuntimeError("simulated restore failure")
        return real_ensure(project_name)

    recovery._session_service.ensure_started = broken  # type: ignore[assignment]

    restored, failed = recovery.restore_sessions()
    assert restored == ("beta",)
    assert failed == ("alpha",)


def test_restore_sessions_empty_when_no_claude_sessions(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    recovery = _build_recovery(conn, projects_root, _FakeTmux())
    restored, failed = recovery.restore_sessions()
    assert restored == ()
    assert failed == ()


# ---- run (full flow) -------------------------------------------------


def test_run_resets_yolo_before_restoring_sessions(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    """Critical ordering guarantee: the YOLO coercion must hit the
    DB before ensure_started reads the mode, otherwise the recycled
    Claude would come back up still armed with
    --dangerously-skip-permissions."""
    _seed_project(conn, "alpha", Mode.YOLO)
    tmux = _FakeTmux()
    recovery = _build_recovery(conn, projects_root, tmux)

    report = recovery.run()

    assert set(report.yolo_resets) == {"alpha"}
    assert set(report.restored_sessions) == {"alpha"}
    # The restored tmux session got the Normal-mode launch flag —
    # no --permission-mode or --dangerously-skip-permissions in the
    # safe-claude argv.
    launches = [text for _, text in tmux.send_text_calls]
    assert any(
        "--permission-mode" not in text
        and "--dangerously-skip-permissions" not in text
        for text in launches
    )


def test_run_report_captures_all_categories(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", Mode.YOLO)
    _seed_project(conn, "beta", Mode.NORMAL)
    _seed_project(conn, "gamma", Mode.STRICT)
    recovery = _build_recovery(conn, projects_root, _FakeTmux())

    report = recovery.run()

    assert set(report.yolo_resets) == {"alpha"}
    assert set(report.restored_sessions) == {"alpha", "beta", "gamma"}
    assert report.failed_sessions == ()
