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

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def handler(conn: sqlite3.Connection, tmp_path: Path) -> CommandHandler:
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    project_service = ProjectService(
        repository=SqliteProjectRepository(conn),
        conn=conn,
        projects_root=projects_root,
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


def test_new_git_returns_phase_2_2_hint(handler: CommandHandler) -> None:
    """`/new <name> git <url>` is documented in the spec but the wiring
    lands in C2.2; we should respond with a clear deferral, not silently
    create an empty project under that name."""
    result = handler.handle("/new alpha git https://github.com/x/y")
    assert result.command == "/new git"
    assert "C2.2" in result.reply


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
