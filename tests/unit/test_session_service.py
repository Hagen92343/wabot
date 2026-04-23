"""Unit tests for whatsbot.application.session_service.

Uses a ``FakeTmuxController`` that records every call and lets us flip
``has_session`` independently of ``new_session`` / ``kill_session``.
The claude-session repository is the real SQLite adapter against
``:memory:`` — cheap enough, and exercises the adapter path by default.
"""

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
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.session_service import SessionService
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession, tmux_session_name
from whatsbot.ports.project_repository import ProjectNotFoundError
from whatsbot.ports.tmux_controller import TmuxError

pytestmark = pytest.mark.unit


@dataclass
class FakeTmuxController:
    """Records all calls and simulates session presence in-memory.

    ``has_session`` returns True iff the name is in ``_alive``. Tests
    can preseed ``_alive`` to mimic "session already running" or let
    ``new_session`` flip it on.
    """

    _alive: set[str] = field(default_factory=set)
    new_session_calls: list[tuple[str, str]] = field(default_factory=list)
    send_text_calls: list[tuple[str, str]] = field(default_factory=list)
    set_status_calls: list[tuple[str, str, str]] = field(default_factory=list)
    kill_session_calls: list[str] = field(default_factory=list)
    raise_on_new: TmuxError | None = None

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: Path | str) -> None:
        if self.raise_on_new is not None:
            raise self.raise_on_new
        self.new_session_calls.append((name, str(cwd)))
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        self.send_text_calls.append((name, text))

    def kill_session(self, name: str) -> bool:
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        names = sorted(self._alive)
        if prefix is not None:
            names = [n for n in names if n.startswith(prefix)]
        return names

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
    *,
    mode: Mode = Mode.NORMAL,
    projects_root: Path | None = None,
) -> None:
    SqliteProjectRepository(conn).create(
        Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=mode,
        )
    )
    if projects_root is not None:
        (projects_root / name).mkdir(exist_ok=True)


def _build_service(
    conn: sqlite3.Connection,
    projects_root: Path,
    tmux: FakeTmuxController,
    *,
    safe_claude_binary: str = "safe-claude",
) -> SessionService:
    return SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=projects_root,
        safe_claude_binary=safe_claude_binary,
    )


# ---- tmux missing: fresh session ---------------------------------------


def test_fresh_start_creates_tmux_and_row(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", projects_root=projects_root)
    tmux = FakeTmuxController()
    svc = _build_service(conn, projects_root, tmux)

    result = svc.ensure_started("alpha")

    # tmux session was created at the project path.
    assert tmux.new_session_calls == [("wb-alpha", str(projects_root / "alpha"))]
    # Launch command went into the pane.
    assert len(tmux.send_text_calls) == 1
    sent_to, sent_text = tmux.send_text_calls[0]
    assert sent_to == "wb-alpha"
    # Fresh session → no --resume.
    assert sent_text == "safe-claude"
    # Status bar was painted green for Normal.
    assert tmux.set_status_calls == [
        ("wb-alpha", "green", "🟢 NORMAL [wb-alpha]")
    ]
    # DB row was persisted.
    persisted = SqliteClaudeSessionRepository(conn).get("alpha")
    assert persisted is not None
    assert persisted.project_name == "alpha"
    assert persisted.session_id == ""
    assert persisted.current_mode is Mode.NORMAL
    # Returned value agrees with DB.
    assert result.project_name == "alpha"


def test_strict_mode_adds_permission_flag(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", mode=Mode.STRICT, projects_root=projects_root)
    tmux = FakeTmuxController()
    svc = _build_service(conn, projects_root, tmux)

    svc.ensure_started("alpha")

    _, sent_text = tmux.send_text_calls[0]
    assert sent_text == "safe-claude --permission-mode dontAsk"
    # Status bar colour tracks the mode.
    assert tmux.set_status_calls[0][1] == "blue"
    assert "🔵 STRICT" in tmux.set_status_calls[0][2]


def test_yolo_mode_uses_dangerous_flag(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", mode=Mode.YOLO, projects_root=projects_root)
    tmux = FakeTmuxController()
    svc = _build_service(conn, projects_root, tmux)

    svc.ensure_started("alpha")

    _, sent_text = tmux.send_text_calls[0]
    assert sent_text == "safe-claude --dangerously-skip-permissions"
    assert tmux.set_status_calls[0][1] == "red"


def test_custom_safe_claude_binary_threads_through(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", projects_root=projects_root)
    tmux = FakeTmuxController()
    svc = _build_service(
        conn,
        projects_root,
        tmux,
        safe_claude_binary="/tmp/stub-claude",
    )

    svc.ensure_started("alpha")

    _, sent_text = tmux.send_text_calls[0]
    assert sent_text == "/tmp/stub-claude"


# ---- tmux alive: no-op launch -----------------------------------------


def test_already_running_does_not_relaunch(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", projects_root=projects_root)
    tmux = FakeTmuxController()
    tmux._alive.add("wb-alpha")
    # Preseed a DB row so we don't go through the "first start" path.
    SqliteClaudeSessionRepository(conn).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-42",
            transcript_path="/tmp/t.jsonl",
            started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            current_mode=Mode.NORMAL,
        )
    )
    svc = _build_service(conn, projects_root, tmux)

    svc.ensure_started("alpha")

    # No new session, no send_text — just a status-bar refresh.
    assert tmux.new_session_calls == []
    assert tmux.send_text_calls == []
    assert len(tmux.set_status_calls) == 1


def test_already_running_backfills_missing_db_row(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", projects_root=projects_root)
    tmux = FakeTmuxController()
    tmux._alive.add("wb-alpha")
    svc = _build_service(conn, projects_root, tmux)

    svc.ensure_started("alpha")

    # Still no launch, but the DB row is now present so later ingest
    # work has somewhere to land.
    assert tmux.new_session_calls == []
    assert tmux.send_text_calls == []
    row = SqliteClaudeSessionRepository(conn).get("alpha")
    assert row is not None
    assert row.current_mode is Mode.NORMAL


# ---- tmux dead, DB row exists: resume ---------------------------------


def test_dead_tmux_with_db_row_resumes(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", projects_root=projects_root)
    SqliteClaudeSessionRepository(conn).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-42",
            transcript_path="/tmp/t.jsonl",
            started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            current_mode=Mode.NORMAL,
        )
    )
    tmux = FakeTmuxController()  # empty _alive: tmux gone
    svc = _build_service(conn, projects_root, tmux)

    svc.ensure_started("alpha")

    _, sent_text = tmux.send_text_calls[0]
    assert sent_text == "safe-claude --resume sess-42"


def test_mode_switch_while_tmux_dead_uses_project_mode(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    # Project row says STRICT but the leftover DB session row still
    # claims NORMAL (mode was flipped while tmux was down). The restart
    # must honour the project mode.
    _seed_project(conn, "alpha", mode=Mode.STRICT, projects_root=projects_root)
    SqliteClaudeSessionRepository(conn).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-42",
            transcript_path="",
            started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            current_mode=Mode.NORMAL,
        )
    )
    tmux = FakeTmuxController()
    svc = _build_service(conn, projects_root, tmux)

    svc.ensure_started("alpha")

    _, sent_text = tmux.send_text_calls[0]
    assert "--permission-mode dontAsk" in sent_text
    # DB row is realigned so the next ensure_started sees the right mode.
    row = SqliteClaudeSessionRepository(conn).get("alpha")
    assert row is not None
    assert row.current_mode is Mode.STRICT


# ---- error paths -------------------------------------------------------


def test_missing_project_raises(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    tmux = FakeTmuxController()
    svc = _build_service(conn, projects_root, tmux)
    with pytest.raises(ProjectNotFoundError):
        svc.ensure_started("ghost")
    # No tmux side-effects on failure.
    assert tmux.new_session_calls == []
    assert tmux.send_text_calls == []


def test_new_session_failure_propagates(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    _seed_project(conn, "alpha", projects_root=projects_root)
    tmux = FakeTmuxController(raise_on_new=TmuxError("boom"))
    svc = _build_service(conn, projects_root, tmux)

    with pytest.raises(TmuxError, match="boom"):
        svc.ensure_started("alpha")

    # No stale DB row left behind.
    assert SqliteClaudeSessionRepository(conn).get("alpha") is None


# ---- naming regression -------------------------------------------------


def test_tmux_session_name_format() -> None:
    assert tmux_session_name("alpha") == "wb-alpha"
