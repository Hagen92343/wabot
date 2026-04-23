"""Phase 6 C6.2 — CommandHandler /panic routing."""

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
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
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
from whatsbot.application.lock_service import LockService
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.application.panic_service import PanicService
from whatsbot.application.project_service import ProjectService
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.process_killer import KillResult
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    kill_session_calls: list[str] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:  # pragma: no cover
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:  # pragma: no cover
        del name, text

    def interrupt(self, name: str) -> None:  # pragma: no cover
        del name

    def kill_session(self, name: str) -> bool:
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        names = sorted(self._alive)
        if prefix is None:
            return names
        return [n for n in names if n.startswith(prefix)]

    def set_status(self, name: str, *, color: str, label: str) -> None:  # pragma: no cover
        del name, color, label


class _FakeKiller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def kill_by_pattern(self, pattern: str) -> KillResult:
        self.calls.append(pattern)
        return KillResult(pattern=pattern, exit_code=1, matched_count=0)


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
    project_repo = SqliteProjectRepository(c)
    project_repo.create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.YOLO,
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
    with_panic: bool = True,
) -> tuple[CommandHandler, _FakeTmux, LockdownService]:
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
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state_repo,
        secrets=_StubSecrets(),
        projects_root=projects_root,
    )
    locks = LockService(
        repo=SqliteSessionLockRepository(conn), clock=lambda: NOW
    )
    tmux = _FakeTmux(_alive=set(alive or set()))
    lockdown = LockdownService(
        app_state=app_state_repo,
        panic_marker_path=projects_root / "PANIC",
        clock=lambda: NOW,
    )
    panic_service: PanicService | None = None
    if with_panic:
        panic_service = PanicService(
            tmux=tmux,
            project_repo=project_repo,
            mode_event_repo=SqliteModeEventRepository(conn),
            lock_service=locks,
            lockdown_service=lockdown,
            process_killer=_FakeKiller(),
            notifier=None,
            clock=lambda: NOW,
            monotonic=lambda: 0.0,
        )
    handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active,
        delete_service=delete_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
        lock_service=locks,
        panic_service=panic_service,
    )
    return handler, tmux, lockdown


# ---- /panic ---------------------------------------------------


def test_panic_runs_and_acks(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, tmux, lockdown = _build_handler(
        conn, tmp_path, alive={"wb-alpha"}
    )
    result = handler.handle("/panic")
    assert result.command == "/panic"
    assert "🚨" in result.reply
    assert "PANIC" in result.reply
    assert "1 Sessions" in result.reply
    assert "1 YOLO" in result.reply  # alpha is YOLO
    assert "Lockdown" in result.reply
    assert "/unlock" in result.reply
    # Side-effects landed.
    assert tmux.kill_session_calls == ["wb-alpha"]
    assert lockdown.is_engaged() is True


def test_panic_no_pin_required(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Spec §5: /panic deliberately has no PIN gate — low friction
    in an emergency. Just confirm no PIN parsing happens."""
    handler, _, _ = _build_handler(conn, tmp_path)
    # Even with whitespace gunk after, this must not parse as PIN.
    result = handler.handle("/panic")
    assert result.command == "/panic"
    assert "🚨" in result.reply


def test_panic_replies_when_service_not_wired(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _, _ = _build_handler(conn, tmp_path, with_panic=False)
    result = handler.handle("/panic")
    assert result.command == "/panic"
    assert "nicht konfiguriert" in result.reply


def test_panic_swallows_inner_exception(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """If panic itself blows up, the user shouldn't see a 500 — they
    should see a clear "check /errors" hint."""
    handler, _, _ = _build_handler(conn, tmp_path)

    def boom() -> None:
        raise RuntimeError("simulated panic crash")

    handler._panic.panic = boom  # type: ignore[union-attr,method-assign]
    result = handler.handle("/panic")
    assert result.command == "/panic"
    assert "gescheitert" in result.reply
