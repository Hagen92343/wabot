"""Unit tests for SqliteAppStateRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_app_state_repository import (
    SqliteAppStateRepository,
)
from whatsbot.ports.app_state_repository import KEY_ACTIVE_PROJECT

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
def repo(conn: sqlite3.Connection) -> SqliteAppStateRepository:
    return SqliteAppStateRepository(conn)


def test_get_returns_none_for_unset_key(repo: SqliteAppStateRepository) -> None:
    assert repo.get(KEY_ACTIVE_PROJECT) is None


def test_set_then_get(repo: SqliteAppStateRepository) -> None:
    repo.set(KEY_ACTIVE_PROJECT, "alpha")
    assert repo.get(KEY_ACTIVE_PROJECT) == "alpha"


def test_set_overwrites_existing_value(repo: SqliteAppStateRepository) -> None:
    repo.set(KEY_ACTIVE_PROJECT, "alpha")
    repo.set(KEY_ACTIVE_PROJECT, "beta")
    assert repo.get(KEY_ACTIVE_PROJECT) == "beta"


def test_delete_returns_true_when_present(repo: SqliteAppStateRepository) -> None:
    repo.set(KEY_ACTIVE_PROJECT, "alpha")
    assert repo.delete(KEY_ACTIVE_PROJECT) is True
    assert repo.get(KEY_ACTIVE_PROJECT) is None


def test_delete_returns_false_when_absent(repo: SqliteAppStateRepository) -> None:
    assert repo.delete(KEY_ACTIVE_PROJECT) is False


def test_keys_isolated(repo: SqliteAppStateRepository) -> None:
    repo.set("foo", "1")
    repo.set("bar", "2")
    assert repo.get("foo") == "1"
    assert repo.get("bar") == "2"
    repo.delete("foo")
    assert repo.get("foo") is None
    assert repo.get("bar") == "2"
