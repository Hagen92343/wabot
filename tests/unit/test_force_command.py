"""Phase 5 C5.4 — CommandHandler /force routing.

Verifies parsing, the PIN gate, and the lock-takeover-then-prompt
sequence. Uses an in-memory tmux fake so we never spawn a real
process.
"""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import SqliteAllowRuleRepository
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
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
from whatsbot.application.force_service import ForceService
from whatsbot.application.lock_service import LockService
from whatsbot.application.project_service import ProjectService
from whatsbot.application.session_service import SessionService
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession
from whatsbot.ports.git_clone import GitClone
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)
PIN = "1234"


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    send_text_calls: list[tuple[str, str]] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        self.send_text_calls.append((name, text))

    def interrupt(self, name: str) -> None:
        del name

    def kill_session(self, name: str) -> bool:
        self._alive.discard(name)
        return True

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        del name, color, label


class _StubGit:
    def clone(
        self, url: str, dest: Path, *, depth: int = 50, timeout_seconds: float = 180.0
    ) -> None:
        del url, depth, timeout_seconds
        dest.mkdir(parents=True, exist_ok=False)


class _StubSecrets:
    def __init__(self, pin: str | None = PIN) -> None:
        self._store: dict[str, str] = {}
        if pin is not None:
            self._store[KEY_PANIC_PIN] = pin

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
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


def _build_handler(
    conn: sqlite3.Connection,
    projects_root: Path,
    *,
    pin: str | None = PIN,
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
    active = ActiveProjectService(app_state=app_state_repo, projects=project_repo)
    secrets = _StubSecrets(pin=pin)
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state_repo,
        secrets=secrets,
        projects_root=projects_root,
    )
    lock_repo = SqliteSessionLockRepository(conn)
    locks = LockService(repo=lock_repo, clock=lambda: NOW)
    tmux = _FakeTmux()
    sessions = SessionService(
        project_repo=project_repo,
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=projects_root,
        lock_service=locks,
    )
    force = ForceService(
        lock_service=locks,
        project_repo=project_repo,
        secrets=secrets,
    )
    handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active,
        delete_service=delete_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
        session_service=sessions,
        lock_service=locks,
        force_service=force,
    )
    return handler, tmux, lock_repo


# ---- happy path ----------------------------------------------------


def test_force_takes_lock_and_sends_prompt(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, lock_repo = _build_handler(conn, tmp_path)

    # Pre-existing local lock that we want to override.
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW - timedelta(seconds=5),
            last_activity_at=NOW - timedelta(seconds=5),
        )
    )

    result = handler.handle(f"/force alpha {PIN} hi there")

    assert result.command == "/force"
    assert "🔓" in result.reply
    assert "alpha" in result.reply
    assert "📨" in result.reply
    assert "hi there" in result.reply  # prompt preview

    # Lock now BOT
    persisted = lock_repo.get("alpha")
    assert persisted is not None
    assert persisted.owner is LockOwner.BOT

    # Prompt actually reached tmux (ZWSP-prefixed).
    pane_writes = [text for _, text in tmux.send_text_calls if "​hi there" in text]
    assert len(pane_writes) == 1


def test_force_prompt_can_contain_spaces_and_more_pins(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The third positional consumes everything to the right — even
    if the user happens to write text that looks like another PIN."""
    handler, tmux, _ = _build_handler(conn, tmp_path)

    result = handler.handle(f"/force alpha {PIN} 9999 do the thing now")
    assert result.command == "/force"
    assert "🔓" in result.reply
    pane_writes = [
        text for _, text in tmux.send_text_calls if "9999 do the thing now" in text
    ]
    assert len(pane_writes) == 1


# ---- failure paths -------------------------------------------------


def test_force_wrong_pin_replies_and_keeps_local_lock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, lock_repo = _build_handler(conn, tmp_path)
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW - timedelta(seconds=5),
            last_activity_at=NOW - timedelta(seconds=5),
        )
    )

    result = handler.handle("/force alpha 9999 hi")
    assert result.command == "/force"
    assert "Falsche PIN" in result.reply

    # Local terminal still owns it.
    persisted = lock_repo.get("alpha")
    assert persisted is not None
    assert persisted.owner is LockOwner.LOCAL
    # No prompt was forwarded.
    assert tmux.send_text_calls == []


def test_force_panic_pin_missing_in_keychain(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path, pin=None)
    result = handler.handle(f"/force alpha {PIN} hi")
    assert result.command == "/force"
    assert "Panic-PIN" in result.reply


def test_force_unknown_project(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path)
    result = handler.handle(f"/force ghost {PIN} hi")
    assert result.command == "/force"
    assert "ghost" in result.reply
    assert "/ls" in result.reply


# ---- argument-parsing fences --------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        "/force",  # bare
        "/force alpha",  # name only
        f"/force alpha {PIN}",  # missing prompt
        f"/force alpha {PIN} ",  # whitespace-only prompt
    ],
)
def test_force_usage_error(
    conn: sqlite3.Connection, tmp_path: Path, args: str
) -> None:
    handler, tmux, lock_repo = _build_handler(conn, tmp_path)
    result = handler.handle(args)
    # Bare "/force" doesn't even hit the prefix branch — it falls through
    # to the pure router which renders a generic help reply. The other
    # under-specified forms hit /force directly with a usage hint.
    if args == "/force":
        # Pure router → not "/force" command, but it must NOT touch state.
        pass
    else:
        assert result.command == "/force"
        assert "Verwendung" in result.reply
    # No state side effects in either case.
    assert lock_repo.get("alpha") is None
    assert tmux.send_text_calls == []


def test_force_invalid_project_name(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path)
    result = handler.handle(f"/force BAD {PIN} hi")
    assert result.command == "/force"
    # InvalidProjectNameError surfaces with ⚠️
    assert "⚠️" in result.reply


# ---- not-configured guard -----------------------------------------


def test_force_replies_when_service_not_wired(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """If the bot was started without a ForceService (e.g. no tmux on
    the box), /force must surface the no-config message instead of
    crashing."""
    project_repo = SqliteProjectRepository(conn)
    project_service = ProjectService(
        repository=project_repo,
        conn=conn,
        projects_root=tmp_path,
        git_clone=_StubGit(),
    )
    allow_service = AllowService(
        rule_repo=SqliteAllowRuleRepository(conn),
        project_repo=project_repo,
        projects_root=tmp_path,
    )
    app_state_repo = SqliteAppStateRepository(conn)
    active = ActiveProjectService(app_state=app_state_repo, projects=project_repo)
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state_repo,
        secrets=_StubSecrets(),
        projects_root=tmp_path,
    )
    handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active,
        delete_service=delete_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
        # No session/lock/force services on purpose.
    )

    result = handler.handle(f"/force alpha {PIN} hi")
    assert result.command == "/force"
    assert "nicht konfiguriert" in result.reply


# ---- compile-time ergonomics --------------------------------------


def test_help_hint_message_includes_pin(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The ``🔒 Terminal aktiv`` hint we render on lock-denied prompts
    must instruct the user with the *real* /force syntax (including
    PIN), not a stale older syntax."""
    handler, _, lock_repo = _build_handler(conn, tmp_path)
    lock_repo.upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW - timedelta(seconds=5),
            last_activity_at=NOW - timedelta(seconds=5),
        )
    )
    result = handler.handle("/p alpha hi")
    assert "🔒" in result.reply
    assert "<PIN>" in result.reply
    assert "<prompt>" in result.reply


# Silence unused-import warnings on the Iterator type alias
_ = (Iterator, GitClone)
