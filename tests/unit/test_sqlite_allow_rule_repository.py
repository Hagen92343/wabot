"""Unit tests for SqliteAllowRuleRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import (
    SqliteAllowRuleRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.domain.allow_rules import AllowRulePattern, AllowRuleSource
from whatsbot.domain.projects import Mode, Project, SourceMode

pytestmark = pytest.mark.unit


def _seed_project(conn: sqlite3.Connection, name: str = "alpha") -> None:
    repo = SqliteProjectRepository(conn)
    repo.create(
        Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    _seed_project(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteAllowRuleRepository:
    return SqliteAllowRuleRepository(conn)


# --- add + list ------------------------------------------------------------


def test_add_then_list(repo: SqliteAllowRuleRepository) -> None:
    pat = AllowRulePattern(tool="Bash", pattern="npm test")
    stored = repo.add("alpha", pat, AllowRuleSource.SMART_DETECTION)
    assert stored.project_name == "alpha"
    assert stored.pattern == pat
    assert stored.source is AllowRuleSource.SMART_DETECTION
    assert stored.id > 0

    rules = repo.list_for_project("alpha")
    assert len(rules) == 1
    assert rules[0].pattern == pat


def test_list_empty_project_returns_empty(repo: SqliteAllowRuleRepository) -> None:
    assert repo.list_for_project("alpha") == []


def test_list_is_in_insertion_order(repo: SqliteAllowRuleRepository) -> None:
    for cmd in ("git status", "npm test", "make build"):
        repo.add(
            "alpha",
            AllowRulePattern(tool="Bash", pattern=cmd),
            AllowRuleSource.MANUAL,
        )
    patterns = [r.pattern.pattern for r in repo.list_for_project("alpha")]
    assert patterns == ["git status", "npm test", "make build"]


def test_add_is_idempotent_on_duplicate(repo: SqliteAllowRuleRepository) -> None:
    pat = AllowRulePattern(tool="Bash", pattern="npm test")
    first = repo.add("alpha", pat, AllowRuleSource.MANUAL)
    second = repo.add("alpha", pat, AllowRuleSource.SMART_DETECTION)
    # Same row returned, no duplicate insert.
    assert first.id == second.id
    assert len(repo.list_for_project("alpha")) == 1


# --- remove ---------------------------------------------------------------


def test_remove_returns_true_when_row_exists(
    repo: SqliteAllowRuleRepository,
) -> None:
    pat = AllowRulePattern(tool="Bash", pattern="npm test")
    repo.add("alpha", pat, AllowRuleSource.MANUAL)
    assert repo.remove("alpha", pat) is True
    assert repo.list_for_project("alpha") == []


def test_remove_returns_false_when_row_absent(
    repo: SqliteAllowRuleRepository,
) -> None:
    pat = AllowRulePattern(tool="Bash", pattern="ghost")
    assert repo.remove("alpha", pat) is False


def test_per_project_isolation(repo: SqliteAllowRuleRepository, conn: sqlite3.Connection) -> None:
    _seed_project(conn, name="beta")
    pat_alpha = AllowRulePattern(tool="Bash", pattern="npm test")
    pat_beta = AllowRulePattern(tool="Bash", pattern="cargo test")
    repo.add("alpha", pat_alpha, AllowRuleSource.MANUAL)
    repo.add("beta", pat_beta, AllowRuleSource.MANUAL)

    alpha_rules = [r.pattern.pattern for r in repo.list_for_project("alpha")]
    beta_rules = [r.pattern.pattern for r in repo.list_for_project("beta")]
    assert alpha_rules == ["npm test"]
    assert beta_rules == ["cargo test"]


# --- DB-level integrity ---------------------------------------------------


def test_invalid_source_rejected_by_check_constraint(
    conn: sqlite3.Connection,
) -> None:
    """Spec §19: source CHECK(IN ('default', 'smart_detection', 'manual'))."""
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO allow_rules"
            "(project_name, tool, pattern, created_at, source) "
            "VALUES (?, 'Bash', 'npm test', '2026-04-22T12:00', 'rocket')",
            ("alpha",),
        )


def test_cascade_delete_removes_rules(
    conn: sqlite3.Connection, repo: SqliteAllowRuleRepository
) -> None:
    """Deleting the project row should cascade allow_rules cleanup."""
    repo.add(
        "alpha",
        AllowRulePattern(tool="Bash", pattern="npm test"),
        AllowRuleSource.MANUAL,
    )
    SqliteProjectRepository(conn).delete("alpha")
    assert repo.list_for_project("alpha") == []
