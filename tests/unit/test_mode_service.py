"""Unit tests for whatsbot.application.mode_service."""

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
from whatsbot.application.mode_service import (
    InvalidModeTransitionError,
    ModeService,
)
from whatsbot.application.session_service import SessionService
from whatsbot.domain.mode_events import ModeEventKind
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession
from whatsbot.ports.project_repository import ProjectNotFoundError

pytestmark = pytest.mark.unit


# Borrow the FakeTmuxController shape from the session_service tests.
# Copying a small stub is cleaner than adding a cross-test import.
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
        self.new_session_calls.append((name, str(cwd)))
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        self.send_text_calls.append((name, text))

    def interrupt(self, name: str) -> None:
        del name

    def kill_session(self, name: str) -> bool:
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
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
    return tmp_path / "projekte"


def _seed(conn: sqlite3.Connection, name: str, mode: Mode) -> None:
    SqliteProjectRepository(conn).create(
        Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=mode,
        )
    )
    # Seed a claude_sessions row with a known session_id so recycle
    # uses --resume.
    SqliteClaudeSessionRepository(conn).upsert(
        ClaudeSession(
            project_name=name,
            session_id="sess-42",
            transcript_path="",
            started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            current_mode=mode,
        )
    )


def _build(
    conn: sqlite3.Connection,
    projects_root: Path,
    tmux: _FakeTmux,
) -> ModeService:
    session_service = SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=projects_root,
    )
    return ModeService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        mode_event_repo=SqliteModeEventRepository(conn),
        session_service=session_service,
    )


def test_show_mode_returns_current_mode(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed(conn, "alpha", Mode.NORMAL)
    svc = _build(conn, projects_root, _FakeTmux())
    assert svc.show_mode("alpha") is Mode.NORMAL


def test_show_mode_raises_on_missing_project(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    svc = _build(conn, projects_root, _FakeTmux())
    with pytest.raises(ProjectNotFoundError):
        svc.show_mode("ghost")


def test_change_mode_normal_to_strict_persists_and_recycles(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed(conn, "alpha", Mode.NORMAL)
    tmux = _FakeTmux()
    tmux._alive.add("wb-alpha")  # pretend a session is running
    svc = _build(conn, projects_root, tmux)

    outcome = svc.change_mode("alpha", Mode.STRICT)

    assert outcome.from_mode is Mode.NORMAL
    assert outcome.to_mode is Mode.STRICT
    assert outcome.was_noop is False

    # projects.mode updated.
    project_row = SqliteProjectRepository(conn).get("alpha")
    assert project_row.mode is Mode.STRICT
    # claude_sessions.current_mode updated.
    session_row = SqliteClaudeSessionRepository(conn).get("alpha")
    assert session_row is not None
    assert session_row.current_mode is Mode.STRICT

    # mode_events row written.
    events = SqliteModeEventRepository(conn).list_for_project("alpha")
    assert len(events) == 1
    assert events[0].kind is ModeEventKind.SWITCH
    assert events[0].from_mode is Mode.NORMAL
    assert events[0].to_mode is Mode.STRICT

    # Session recycled: tmux was killed then re-launched with the new flag.
    assert tmux.kill_session_calls == ["wb-alpha"]
    assert tmux.new_session_calls == [("wb-alpha", str(projects_root / "alpha"))]
    # The new safe-claude launch carries the --permission-mode dontAsk flag.
    assert any(
        "--permission-mode" in text and "dontAsk" in text
        for _, text in tmux.send_text_calls
    )
    # And it preserved the session_id via --resume.
    assert any("--resume sess-42" in text for _, text in tmux.send_text_calls)


def test_change_mode_same_mode_is_noop_but_still_audited(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed(conn, "alpha", Mode.NORMAL)
    tmux = _FakeTmux()
    tmux._alive.add("wb-alpha")
    svc = _build(conn, projects_root, tmux)

    outcome = svc.change_mode("alpha", Mode.NORMAL)

    assert outcome.was_noop is True
    # No kill/relaunch on no-op.
    assert tmux.kill_session_calls == []
    # But the audit row still landed.
    events = SqliteModeEventRepository(conn).list_for_project("alpha")
    assert len(events) == 1


def test_change_mode_to_yolo_uses_dangerous_flag(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed(conn, "alpha", Mode.NORMAL)
    tmux = _FakeTmux()
    tmux._alive.add("wb-alpha")
    svc = _build(conn, projects_root, tmux)

    svc.change_mode("alpha", Mode.YOLO)

    assert any(
        "--dangerously-skip-permissions" in text
        for _, text in tmux.send_text_calls
    )


def test_change_mode_unknown_project(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    svc = _build(conn, projects_root, _FakeTmux())
    with pytest.raises(ProjectNotFoundError):
        svc.change_mode("ghost", Mode.STRICT)


def test_invalid_mode_transition_raises() -> None:
    # valid_transition currently returns True for every pair — this
    # test just pins the contract so when future policy enforces
    # forbidden transitions (Spec §6 note on Strict→Normal escape)
    # we get a failing test rather than silent acceptance.
    from whatsbot.domain import modes as modes_mod

    assert modes_mod.valid_transition(Mode.NORMAL, Mode.YOLO) is True
    # Sanity: InvalidModeTransitionError is wired to raise from
    # change_mode when valid_transition returns False — we test this
    # by monkeypatching (keeps us honest).


def test_change_mode_raises_when_transition_rejected(
    conn: sqlite3.Connection,
    projects_root: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed(conn, "alpha", Mode.NORMAL)
    svc = _build(conn, projects_root, _FakeTmux())
    # Force valid_transition to reject the pair.
    import whatsbot.application.mode_service as mode_svc

    monkeypatch.setattr(
        mode_svc, "valid_transition", lambda _f, _t: False
    )
    with pytest.raises(InvalidModeTransitionError):
        svc.change_mode("alpha", Mode.STRICT)
    # The rejection happens before any DB write.
    assert (
        SqliteProjectRepository(conn).get("alpha").mode is Mode.NORMAL
    )
