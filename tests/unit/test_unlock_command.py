"""Phase 6 C6.6 — CommandHandler /unlock + Lockdown filter."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
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
from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import AllowService
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.delete_service import DeleteService
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.application.project_service import ProjectService
from whatsbot.application.unlock_service import UnlockService
from whatsbot.domain.lockdown import LOCKDOWN_REASON_PANIC
from whatsbot.ports.secrets_provider import KEY_PANIC_PIN, SecretNotFoundError

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


class _StubGit:
    def clone(
        self, url: str, dest: Path, *, depth: int = 50, timeout_seconds: float = 180.0
    ) -> None:  # pragma: no cover
        del url, depth, timeout_seconds
        dest.mkdir(parents=True, exist_ok=False)


class _StubSecrets:
    def __init__(self, pin: str | None = "1234") -> None:
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


def _build_handler(
    conn: sqlite3.Connection,
    projects_root: Path,
    *,
    pre_engaged: bool = False,
    pin: str | None = "1234",
    with_unlock: bool = True,
    with_lockdown: bool = True,
) -> tuple[CommandHandler, LockdownService]:
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
    secrets = _StubSecrets(pin=pin)
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state_repo,
        secrets=secrets,
        projects_root=projects_root,
    )
    lockdown = LockdownService(
        app_state=app_state_repo,
        panic_marker_path=projects_root / "PANIC",
        clock=lambda: NOW,
    )
    if pre_engaged:
        lockdown.engage(reason=LOCKDOWN_REASON_PANIC, engaged_by="panic")
    unlock_service = (
        UnlockService(lockdown_service=lockdown, secrets=secrets)
        if with_unlock
        else None
    )
    handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active,
        delete_service=delete_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
        unlock_service=unlock_service,
        lockdown_service=lockdown if with_lockdown else None,
    )
    return handler, lockdown


# ---- /unlock happy path -----------------------------------------


def test_unlock_with_correct_pin_clears_lockdown(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, lockdown = _build_handler(conn, tmp_path, pre_engaged=True)
    result = handler.handle("/unlock 1234")
    assert result.command == "/unlock"
    assert "🔓" in result.reply
    assert "aufgehoben" in result.reply
    assert lockdown.is_engaged() is False


def test_unlock_when_not_engaged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=False)
    result = handler.handle("/unlock 1234")
    assert result.command == "/unlock"
    assert "nicht im Lockdown" in result.reply


# ---- /unlock failures -------------------------------------------


def test_unlock_wrong_pin_keeps_lockdown(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, lockdown = _build_handler(conn, tmp_path, pre_engaged=True)
    result = handler.handle("/unlock 9999")
    assert result.command == "/unlock"
    assert "Falsche PIN" in result.reply
    assert lockdown.is_engaged() is True


def test_unlock_missing_panic_pin(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _ = _build_handler(
        conn, tmp_path, pre_engaged=True, pin=None
    )
    result = handler.handle("/unlock 1234")
    assert result.command == "/unlock"
    assert "Panic-PIN" in result.reply


def test_unlock_without_pin_arg_shows_usage(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=False)
    result = handler.handle("/unlock")
    assert result.command == "/unlock"
    assert "Verwendung" in result.reply


def test_unlock_no_service_wired(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _ = _build_handler(
        conn, tmp_path, with_unlock=False, with_lockdown=False
    )
    result = handler.handle("/unlock 1234")
    assert "nicht konfiguriert" in result.reply


# ---- Lockdown filter -------------------------------------------


def test_lockdown_blocks_arbitrary_commands(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=True)
    for cmd in ("/ls", "/new alpha", "/p alpha", "/mode strict"):
        result = handler.handle(cmd)
        assert "🔒" in result.reply, f"{cmd!r} should be blocked"
        assert "Lockdown" in result.reply
        assert "/unlock" in result.reply


def test_lockdown_blocks_bare_prompts(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A non-slash bare prompt should also be blocked — that's the
    most dangerous thing an attacker could do with a stolen handset."""
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=True)
    result = handler.handle("dangerous unfiltered prompt")
    assert "🔒" in result.reply


def test_lockdown_allows_unlock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Lockdown must not block /unlock — that would be a deadlock."""
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=True)
    result = handler.handle("/unlock 1234")
    assert "🔓" in result.reply
    assert "🔒" not in result.reply


def test_lockdown_allows_diagnostics(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The user wants to verify it's really the bot before tipping
    the PIN — /help, /ping, /status stay open."""
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=True)
    for cmd in ("/help", "/ping", "/status"):
        result = handler.handle(cmd)
        assert "🔒" not in result.reply, (
            f"{cmd!r} should be allowed during lockdown"
        )


def test_filter_no_op_when_lockdown_not_engaged(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    handler, _ = _build_handler(conn, tmp_path, pre_engaged=False)
    result = handler.handle("/ls")
    assert "🔒" not in result.reply


def test_filter_no_op_when_lockdown_service_not_wired(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Older test paths build a CommandHandler without LockdownService —
    the filter must never crash on a None service."""
    handler, _ = _build_handler(
        conn,
        tmp_path,
        with_lockdown=False,
        with_unlock=False,
    )
    result = handler.handle("/ls")
    assert "🔒" not in result.reply
