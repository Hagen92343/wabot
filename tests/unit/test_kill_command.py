"""Phase 6 C6.1 — CommandHandler /stop + /kill routing."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import SqliteAllowRuleRepository
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_pending_delete_repository import (
    SqlitePendingDeleteRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import AllowService
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.delete_service import DeleteService
from whatsbot.application.kill_service import KillService
from whatsbot.application.lock_service import LockService
from whatsbot.application.project_service import ProjectService
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    interrupt_calls: list[str] = field(default_factory=list)
    kill_session_calls: list[str] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:  # pragma: no cover
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:  # pragma: no cover
        del name, text

    def interrupt(self, name: str) -> None:
        self.interrupt_calls.append(name)

    def kill_session(self, name: str) -> bool:
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        del name, color, label


class _StubGit:
    def clone(
        self, url: str, dest: Path, *, depth: int = 50, timeout_seconds: float = 180.0
    ) -> None:  # pragma: no cover
        del url, depth, timeout_seconds
        dest.mkdir(parents=True, exist_ok=False)


class _StubSecrets:
    def __init__(self) -> None:
        self._store = {KEY_PANIC_PIN: "1234"}

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


def _build_handler(
    conn: sqlite3.Connection,
    projects_root: Path,
    *,
    alive: set[str] | None = None,
    with_lock: bool = False,
    set_active: str | None = "alpha",
    with_kill: bool = True,
) -> tuple[CommandHandler, _FakeTmux, SqliteSessionLockRepository]:
    project_repo = SqliteProjectRepository(conn)
    project_service = ProjectService(
        repository=project_repo,
        conn=conn,
        projects_root=projects_root,
        git_clone=_StubGit(),
    )
    allow_service = AllowService(
        rule_repo=SqliteAllowRuleRepository(conn),
        project_repo=project_repo,
        projects_root=projects_root,
    )
    app_state_repo = SqliteAppStateRepository(conn)
    active = ActiveProjectService(
        app_state=app_state_repo, projects=project_repo
    )
    if set_active is not None:
        active.set_active(set_active)
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state_repo,
        secrets=_StubSecrets(),
        projects_root=projects_root,
    )
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
    tmux = _FakeTmux(_alive=set(alive or set()))
    kill_service: KillService | None = None
    if with_kill:
        kill_service = KillService(tmux=tmux, lock_service=locks)
    handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active,
        delete_service=delete_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
        lock_service=locks,
        kill_service=kill_service,
    )
    return handler, tmux, lock_repo


# ---- /stop ---------------------------------------------------------


def test_stop_with_named_project(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, _ = _build_handler(conn, tmp_path, alive={"wb-alpha"})
    result = handler.handle("/stop alpha")
    assert result.command == "/stop"
    assert "🛑" in result.reply
    assert "alpha" in result.reply
    assert tmux.interrupt_calls == ["wb-alpha"]


def test_stop_uses_active_when_no_arg(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, _ = _build_handler(conn, tmp_path, alive={"wb-alpha"})
    result = handler.handle("/stop")
    assert result.command == "/stop"
    assert "🛑" in result.reply
    assert tmux.interrupt_calls == ["wb-alpha"]


def test_stop_no_active_no_arg(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, _ = _build_handler(
        conn, tmp_path, set_active=None, alive={"wb-alpha"}
    )
    result = handler.handle("/stop")
    assert result.command == "/stop"
    assert "kein aktives Projekt" in result.reply
    assert tmux.interrupt_calls == []


def test_stop_session_dead_friendly_reply(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, _ = _build_handler(conn, tmp_path, alive=set())
    result = handler.handle("/stop alpha")
    assert result.command == "/stop"
    assert "keine aktive Session" in result.reply
    assert tmux.interrupt_calls == []


def test_stop_invalid_name(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path, set_active=None)
    result = handler.handle("/stop BAD")
    assert result.command == "/stop"
    assert "⚠️" in result.reply


# ---- /kill ---------------------------------------------------------


def test_kill_destroys_and_releases(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, lock_repo = _build_handler(
        conn, tmp_path, alive={"wb-alpha"}, with_lock=True
    )
    result = handler.handle("/kill alpha")
    assert result.command == "/kill"
    assert "🪓" in result.reply
    assert "Lock freigegeben" in result.reply
    assert tmux.kill_session_calls == ["wb-alpha"]
    assert lock_repo.get("alpha") is None


def test_kill_uses_active_when_no_arg(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, _ = _build_handler(conn, tmp_path, alive={"wb-alpha"})
    result = handler.handle("/kill")
    assert tmux.kill_session_calls == ["wb-alpha"]
    assert result.command == "/kill"


def test_kill_session_dead(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path, alive=set())
    result = handler.handle("/kill alpha")
    assert result.command == "/kill"
    assert "keine aktive Session" in result.reply


def test_kill_no_lock_suffix_when_nothing_to_release(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """If there was no lock to release, the user shouldn't see the
    'Lock freigegeben' suffix — only the kill confirmation."""
    handler, _, _ = _build_handler(conn, tmp_path, alive={"wb-alpha"})
    result = handler.handle("/kill alpha")
    assert "Lock freigegeben" not in result.reply
    assert "🪓" in result.reply


# ---- service-not-wired guard --------------------------------------


def test_stop_replies_when_service_not_wired(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path, with_kill=False)
    result = handler.handle("/stop alpha")
    assert result.command == "/stop"
    assert "nicht konfiguriert" in result.reply


def test_kill_replies_when_service_not_wired(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path, with_kill=False)
    result = handler.handle("/kill alpha")
    assert result.command == "/kill"
    assert "nicht konfiguriert" in result.reply
