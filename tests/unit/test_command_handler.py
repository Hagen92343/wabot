"""Unit tests for whatsbot.application.command_handler."""

from __future__ import annotations

import sqlite3
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import SqliteAllowRuleRepository
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import AllowService
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
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


@pytest.fixture
def handler(
    conn: sqlite3.Connection,
    projects_root: Path,
    git_clone: GitClone,
) -> CommandHandler:
    project_repo = SqliteProjectRepository(conn)
    project_service = ProjectService(
        repository=project_repo,
        conn=conn,
        projects_root=projects_root,
        git_clone=git_clone,
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
    return CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active_project,
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


def test_new_git_clone_failure_surfaces(conn: sqlite3.Connection, projects_root: Path) -> None:
    """When the GitClone adapter raises, the handler must surface the error
    instead of silently swallowing it."""
    failing = StubGitClone(should_fail=True, fail_message="repo not found")
    project_repo = SqliteProjectRepository(conn)
    svc = ProjectService(
        repository=project_repo,
        conn=conn,
        projects_root=projects_root,
        git_clone=failing,
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
    h = CommandHandler(
        project_service=svc,
        allow_service=allow_service,
        active_project=active_project,
        version="0.1.0",
        started_at_monotonic=time.monotonic(),
        env="test",
    )
    result = h.handle("/new alpha git https://github.com/x/y")
    assert result.command == "/new git"
    assert "git clone fehlgeschlagen" in result.reply
    assert not (projects_root / "alpha").exists()


# --- /p (active project) ---------------------------------------------------


def test_p_shows_no_active_when_none_set(handler: CommandHandler) -> None:
    result = handler.handle("/p")
    assert result.command == "/p"
    assert "kein aktives Projekt" in result.reply


def test_p_sets_active_for_existing_project(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    result = handler.handle("/p alpha")
    assert result.command == "/p"
    assert "alpha" in result.reply
    # Now /p without arg returns the active.
    follow_up = handler.handle("/p")
    assert "alpha" in follow_up.reply


def test_p_rejects_unknown_project(handler: CommandHandler) -> None:
    result = handler.handle("/p ghost")
    assert "⚠️" in result.reply
    assert "ghost" in result.reply


def test_ls_marks_active_project(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/new beta")
    handler.handle("/p alpha")
    result = handler.handle("/ls")
    # The format_listing helper prints "▶" next to the active project.
    line_with_marker = next(line for line in result.reply.splitlines() if "▶" in line)
    assert "alpha" in line_with_marker
    assert "beta" not in line_with_marker


# --- /allow + /deny + /allowlist ------------------------------------------


def test_allow_requires_active_project(handler: CommandHandler) -> None:
    result = handler.handle("/allow Bash(npm test)")
    assert result.command == "/allow"
    assert "kein aktives Projekt" in result.reply


def test_allow_adds_manual_rule(handler: CommandHandler, projects_root: Path) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    result = handler.handle("/allow Bash(echo hi)")
    assert result.command == "/allow"
    assert "Bash(echo hi)" in result.reply

    # settings.json must be in sync.
    import json

    settings = json.loads((projects_root / "alpha" / ".claude" / "settings.json").read_text())
    assert "Bash(echo hi)" in settings["permissions"]["allow"]


def test_allow_rejects_invalid_rule(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    result = handler.handle("/allow garbage")
    assert "⚠️" in result.reply


def test_deny_removes_manual_rule(handler: CommandHandler, projects_root: Path) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    handler.handle("/allow Bash(echo hi)")
    result = handler.handle("/deny Bash(echo hi)")
    assert result.command == "/deny"
    assert "🗑" in result.reply

    import json

    settings = json.loads((projects_root / "alpha" / ".claude" / "settings.json").read_text())
    assert settings["permissions"]["allow"] == []


def test_deny_warns_when_rule_absent(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    result = handler.handle("/deny Bash(ghost)")
    assert "⚠️" in result.reply


def test_allowlist_empty(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    result = handler.handle("/allowlist")
    assert "noch keine Allow-Rules" in result.reply


def test_allowlist_groups_by_source(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    handler.handle("/allow Bash(npm test)")
    handler.handle("/allow Bash(make build)")
    result = handler.handle("/allowlist")
    assert "[manual]" in result.reply
    assert "Bash(npm test)" in result.reply
    assert "Bash(make build)" in result.reply


# --- /allow batch approve / review ----------------------------------------


def test_batch_review_when_no_suggestions(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    result = handler.handle("/allow batch review")
    assert "keine Vorschlaege" in result.reply


def test_batch_approve_when_no_suggestions(handler: CommandHandler) -> None:
    handler.handle("/new alpha")
    handler.handle("/p alpha")
    result = handler.handle("/allow batch approve")
    assert "⚠️" in result.reply
    assert "Keine Vorschlaege" in result.reply


def test_batch_review_lists_git_clone_suggestions(handler: CommandHandler) -> None:
    """After /new git, the suggested-rules.json carries 12 patterns
    (5 npm + 7 git) — review must list all of them."""
    handler.handle("/new alpha git https://github.com/o/r")
    handler.handle("/p alpha")
    result = handler.handle("/allow batch review")
    assert "Vorschlaege fuer 'alpha'" in result.reply
    assert "12" in result.reply  # the count
    assert "Bash(npm test)" in result.reply
    assert "Bash(git status)" in result.reply


def test_batch_approve_persists_and_clears(handler: CommandHandler, projects_root: Path) -> None:
    handler.handle("/new alpha git https://github.com/o/r")
    handler.handle("/p alpha")
    approve = handler.handle("/allow batch approve")
    assert "12 neue Rules" in approve.reply

    # suggested-rules.json gone after approve.
    assert not (projects_root / "alpha" / ".whatsbot" / "suggested-rules.json").exists()

    # /allowlist now shows 12 entries under smart_detection.
    listing = handler.handle("/allowlist").reply
    assert "[smart_detection]" in listing
    for needle in ("Bash(npm test)", "Bash(git status)", "Bash(git fetch *)"):
        assert needle in listing


def test_batch_approve_is_idempotent(handler: CommandHandler, projects_root: Path) -> None:
    """Running approve twice must not double-write rules — but the second
    call also has no suggestions, so it raises NoSuggestedRulesError."""
    handler.handle("/new alpha git https://github.com/o/r")
    handler.handle("/p alpha")
    handler.handle("/allow batch approve")
    second = handler.handle("/allow batch approve")
    assert "⚠️" in second.reply
    # Rules from the first approve are still there exactly once.
    listing = handler.handle("/allowlist").reply
    assert listing.count("Bash(npm test)") == 1


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
