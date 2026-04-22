"""Unit tests for whatsbot.application.command_handler."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.project_service import ProjectService
from whatsbot.ports.git_clone import GitClone, GitCloneError

pytestmark = pytest.mark.unit


class StubGitClone:
    """Test double for GitClone — fakes a successful clone by writing a
    fixture file layout into ``dest``. Tests can opt into failure with
    ``StubGitClone(should_fail=True)``."""

    def __init__(
        self,
        *,
        layout: dict[str, str] | None = None,
        should_fail: bool = False,
        fail_message: str = "stub clone failure",
    ) -> None:
        # Default layout: a tiny npm-style repo so smart-detection has
        # something to detect on.
        self._layout = (
            layout
            if layout is not None
            else {
                "package.json": '{"name":"stub","version":"0.0.0"}',
                "README.md": "stub repo for tests",
                ".git/config": "[core]\nrepositoryformatversion = 0\n",
            }
        )
        self._should_fail = should_fail
        self._fail_message = fail_message
        self.calls: list[tuple[str, Path]] = []

    def clone(
        self, url: str, dest: Path, *, depth: int = 50, timeout_seconds: float = 180.0
    ) -> None:
        self.calls.append((url, dest))
        if self._should_fail:
            raise GitCloneError(self._fail_message)
        dest.mkdir(parents=True, exist_ok=False)
        for rel_path, content in self._layout.items():
            file_path = dest / rel_path
            file_path.parent.mkdir(parents=True, exist_ok=True)
            file_path.write_text(content, encoding="utf-8")


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def git_clone() -> GitClone:
    return StubGitClone()


@pytest.fixture
def handler(conn: sqlite3.Connection, tmp_path: Path, git_clone: GitClone) -> CommandHandler:
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    project_service = ProjectService(
        repository=SqliteProjectRepository(conn),
        conn=conn,
        projects_root=projects_root,
        git_clone=git_clone,
    )
    return CommandHandler(
        project_service=project_service,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
    )


# --- pass-through to phase-1 commands --------------------------------------


def test_ping_still_works(handler: CommandHandler) -> None:
    result = handler.handle("/ping")
    assert result.command == "/ping"
    assert "pong" in result.reply


def test_status_still_works(handler: CommandHandler) -> None:
    result = handler.handle("/status")
    assert result.command == "/status"
    assert "test" in result.reply  # env tag


def test_help_still_works(handler: CommandHandler) -> None:
    result = handler.handle("/help")
    assert result.command == "/help"


# --- /new ------------------------------------------------------------------


def test_new_creates_empty_project(handler: CommandHandler) -> None:
    result = handler.handle("/new alpha")
    assert result.command == "/new"
    assert "alpha" in result.reply
    assert "✅" in result.reply or "angelegt" in result.reply


def test_new_appears_in_subsequent_ls(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    listing = handler.handle("/ls")
    assert listing.command == "/ls"
    assert "alpha" in listing.reply


def test_new_rejects_invalid_name(handler: CommandHandler) -> None:
    # Single token, but uppercase is invalid → the validator rejects it
    # rather than the handler's "wrong arity" branch.
    result = handler.handle("/new BAD")
    assert result.command == "/new"
    assert "⚠️" in result.reply
    assert "BAD" in result.reply


def test_new_rejects_duplicate(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    result = handler.handle("/new alpha")
    assert "existiert" in result.reply.lower()


def test_new_with_no_args_returns_usage(handler: CommandHandler) -> None:
    result = handler.handle("/new")
    # `/new` alone does NOT match the `/new ` prefix; falls through as unknown
    # command. That's acceptable — phase-1 commands.route handles the friendly
    # hint and tells them to use /help.
    assert result.command == "<unknown>"


def test_new_with_too_many_args_returns_usage(handler: CommandHandler) -> None:
    result = handler.handle("/new alpha extra")
    assert result.command == "/new"
    assert "Verwendung" in result.reply


def test_new_git_clones_and_runs_smart_detection(handler: CommandHandler) -> None:
    """The default StubGitClone drops a package.json + .git into dest, so
    smart-detection should suggest both npm and git rules."""
    result = handler.handle("/new alpha git https://github.com/octocat/Hello-World")
    assert result.command == "/new git"
    assert "alpha" in result.reply
    assert "geklont" in result.reply
    assert "package.json" in result.reply
    assert ".git" in result.reply


def test_new_git_rejects_disallowed_url(handler: CommandHandler) -> None:
    result = handler.handle("/new alpha git https://evil.example.com/x/y")
    assert result.command == "/new git"
    assert "🚫" in result.reply
    assert "nicht erlaubt" in result.reply


def test_new_git_rejects_invalid_name(handler: CommandHandler) -> None:
    result = handler.handle("/new BAD git https://github.com/x/y")
    assert result.command == "/new git"
    assert "⚠️" in result.reply


def test_new_git_clone_failure_surfaces(conn: sqlite3.Connection, tmp_path: Path) -> None:
    """When the GitClone adapter raises, the handler must surface the error
    instead of silently swallowing it."""
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    failing = StubGitClone(should_fail=True, fail_message="repo not found")
    svc = ProjectService(
        repository=SqliteProjectRepository(conn),
        conn=conn,
        projects_root=projects_root,
        git_clone=failing,
    )
    h = CommandHandler(
        project_service=svc,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
    )
    result = h.handle("/new alpha git https://github.com/x/y")
    assert result.command == "/new git"
    assert "git clone fehlgeschlagen" in result.reply
    assert not (projects_root / "alpha").exists()


# --- /ls -------------------------------------------------------------------


def test_ls_empty(handler: CommandHandler) -> None:
    result = handler.handle("/ls")
    assert result.command == "/ls"
    assert "noch keine Projekte" in result.reply


def test_ls_shows_multiple_projects_alphabetical(handler: CommandHandler) -> None:
    for name in ("zeta", "alpha", "mu"):
        handler.handle(f"/new {name}")
    result = handler.handle("/ls")
    # alphabetical
    body = result.reply
    assert body.index("alpha") < body.index("mu") < body.index("zeta")
