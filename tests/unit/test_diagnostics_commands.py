"""Unit tests for the CommandHandler routes wired by Phase 8 C8.2 —
/log, /errors, /ps, /update.

The fixture builds a CommandHandler with just the bits these
commands touch (project store + active project + a fake
DiagnosticsService). Everything else is left unwired — the router
still handles /ping etc., but we don't exercise it.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

from whatsbot.adapters.sqlite_allow_rule_repository import (
    SqliteAllowRuleRepository,
)
from whatsbot.adapters.sqlite_app_state_repository import (
    SqliteAppStateRepository,
)
from whatsbot.adapters.sqlite_pending_delete_repository import (
    SqlitePendingDeleteRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_repo import apply_schema
from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import AllowService
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.delete_service import DeleteService
from whatsbot.application.diagnostics_service import (
    DiagnosticsService,
    SessionSnapshot,
)
from whatsbot.application.project_service import ProjectService
from whatsbot.domain.locks import LockOwner
from whatsbot.domain.log_events import LogEntry
from whatsbot.domain.projects import Mode
from whatsbot.ports.secrets_provider import SecretNotFoundError


class _StubGit:
    def clone(self, url: str, dest: Path) -> None:  # pragma: no cover
        return None


class _StubSecrets:
    def get(self, key: str) -> str:
        raise SecretNotFoundError(key)

    def set(self, key: str, value: str) -> None:
        return None

    def rotate(self, key: str, new_value: str) -> None:
        return None


class FakeLogReader:
    def __init__(self, entries: list[LogEntry]) -> None:
        self._entries = entries

    def read_tail(self, *, max_lines: int) -> list[LogEntry]:
        return list(self._entries)


@pytest.fixture
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    apply_schema(c)
    return c


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


def _build_handler(
    conn: sqlite3.Connection,
    projects_root: Path,
    *,
    diagnostics: DiagnosticsService | None,
) -> CommandHandler:
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
    active_project = ActiveProjectService(
        app_state=SqliteAppStateRepository(conn),
        projects=project_repo,
    )
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=SqliteAppStateRepository(conn),
        secrets=_StubSecrets(),
        projects_root=projects_root,
    )
    return CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active_project,
        delete_service=delete_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
        diagnostics_service=diagnostics,
    )


# ---- /log -----------------------------------------------------------


def test_log_without_args_returns_usage_hint(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/log")

    assert result.command == "/log"
    assert "Verwendung" in result.reply
    assert "<msg_id>" in result.reply


def test_log_with_blank_arg_still_returns_hint(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/log    ")

    assert result.command == "/log"
    assert "Verwendung" in result.reply


def test_log_surfaces_trace_events(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    entries = [
        LogEntry(event="webhook_in", msg_id="mX", ts="t1", level="INFO"),
        LogEntry(event="other", msg_id="mY", ts="t2", level="INFO"),
        LogEntry(event="deliver", msg_id="mX", ts="t3", level="INFO"),
    ]
    diag = DiagnosticsService(log_reader=FakeLogReader(entries))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/log mX")

    assert result.command == "/log"
    assert "Trace msg_id=mX" in result.reply
    assert "webhook_in" in result.reply
    assert "deliver" in result.reply
    # The "other" message-id must not leak in.
    assert "other" not in result.reply


def test_log_no_matching_msg_id(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/log unknown-id")

    assert result.command == "/log"
    assert "kein Trace" in result.reply


def test_log_without_diagnostics_returns_unavailable(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    handler = _build_handler(conn, projects_root, diagnostics=None)

    result = handler.handle("/log some-id")

    assert result.command == "/log"
    assert "⚠️" in result.reply


# ---- /errors --------------------------------------------------------


def test_errors_with_no_failures_is_friendly(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/errors")

    assert result.command == "/errors"
    assert "keine Fehler" in result.reply


def test_errors_surfaces_warnings_and_errors(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    entries = [
        LogEntry(event="circuit_opened", level="WARNING", ts="t1"),
        LogEntry(event="happy_path_noise", level="INFO", ts="t2"),
        LogEntry(event="write_blocked", level="ERROR", ts="t3"),
    ]
    diag = DiagnosticsService(log_reader=FakeLogReader(entries))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/errors")

    assert "circuit_opened" in result.reply
    assert "write_blocked" in result.reply
    # INFO-level rows must not leak into /errors.
    assert "happy_path_noise" not in result.reply


# ---- /ps ------------------------------------------------------------


def test_ps_without_active_sessions(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/ps")

    assert result.command == "/ps"
    assert "keine aktiven" in result.reply


def test_ps_with_snapshot_renders_details(
    conn: sqlite3.Connection, projects_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    # Monkeypatch the service to return a canned snapshot — exercises
    # the CommandHandler's delegation to format_sessions without
    # needing a full ClaudeSession DB fixture.
    monkeypatch.setattr(
        diag,
        "active_sessions",
        lambda: [
            SessionSnapshot(
                project_name="alpha",
                mode=Mode.NORMAL,
                tmux_alive=True,
                turns_count=7,
                tokens_used=40000,
                context_fill_ratio=0.20,
                lock_owner=LockOwner.BOT,
            )
        ],
    )
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/ps")

    assert "alpha" in result.reply
    assert "turn 7" in result.reply
    assert "20%" in result.reply


# ---- /update --------------------------------------------------------


def test_update_describes_manual_procedure(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    diag = DiagnosticsService(log_reader=FakeLogReader([]))
    handler = _build_handler(conn, projects_root, diagnostics=diag)

    result = handler.handle("/update")

    assert result.command == "/update"
    assert "manuell" in result.reply


def test_update_without_diagnostics_still_answers(
    conn: sqlite3.Connection, projects_root: Path
) -> None:
    handler = _build_handler(conn, projects_root, diagnostics=None)

    result = handler.handle("/update")

    assert result.command == "/update"
    assert "manuell" in result.reply
