"""Unit tests for sqlite_repo: connect, schema, integrity-check, restore."""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo

pytestmark = pytest.mark.unit


# --- connect() / PRAGMAs ----------------------------------------------------


def test_connect_creates_parent_dir(tmp_db_path: Path) -> None:
    nested = tmp_db_path.parent / "deep" / "nested" / "state.db"
    conn = sqlite_repo.connect(nested)
    try:
        assert nested.parent.is_dir()
    finally:
        conn.close()


def test_connect_applies_all_four_pragmas(tmp_db_path: Path) -> None:
    conn = sqlite_repo.connect(tmp_db_path)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        # synchronous: 0=OFF, 1=NORMAL, 2=FULL, 3=EXTRA
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_connect_returns_row_factory(tmp_db_path: Path) -> None:
    conn = sqlite_repo.connect(tmp_db_path)
    try:
        assert conn.row_factory is sqlite3.Row
    finally:
        conn.close()


# --- schema + integrity_check ----------------------------------------------


EXPECTED_TABLES = {
    "projects",
    "claude_sessions",
    "session_locks",
    "pending_deletes",
    "pending_confirmations",
    "max_limits",
    "pending_outputs",
    "app_state",
    "mode_events",
    "allow_rules",
}

EXPECTED_INDEXES = {
    "idx_locks_activity",
    "idx_limits_reset",
    "idx_pending_deadline",
    "idx_mode_events_ts",
    "idx_allow_rules_project",
}


def test_apply_schema_creates_all_expected_tables(tmp_db_path: Path) -> None:
    conn = sqlite_repo.connect(tmp_db_path)
    try:
        sqlite_repo.apply_schema(conn)
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn.close()


def test_apply_schema_creates_all_expected_indexes(tmp_db_path: Path) -> None:
    conn = sqlite_repo.connect(tmp_db_path)
    try:
        sqlite_repo.apply_schema(conn)
        indexes = {
            row[0]
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type='index'")
            if not row[0].startswith("sqlite_")  # auto-indexes
        }
        assert EXPECTED_INDEXES.issubset(indexes)
    finally:
        conn.close()


def test_projects_mode_check_constraint_rejects_invalid_value(
    tmp_db_path: Path,
) -> None:
    """Spec §19: projects.mode CHECK(IN ('normal', 'strict', 'yolo'))."""
    conn = sqlite_repo.connect(tmp_db_path)
    try:
        sqlite_repo.apply_schema(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO projects(name, source_mode, created_at, mode) "
                "VALUES (?, 'empty', '2026-01-01', 'rocket')",
                ("p",),
            )
    finally:
        conn.close()


def test_integrity_check_returns_ok_for_fresh_db(tmp_db_path: Path) -> None:
    conn = sqlite_repo.connect(tmp_db_path)
    try:
        sqlite_repo.apply_schema(conn)
        assert sqlite_repo.integrity_check(conn) == "ok"
    finally:
        conn.close()


# --- backup discovery + restore --------------------------------------------


def test_latest_backup_returns_none_when_dir_missing(tmp_backup_dir: Path) -> None:
    assert sqlite_repo.latest_backup(tmp_backup_dir) is None


def test_latest_backup_returns_none_when_dir_empty(tmp_backup_dir: Path) -> None:
    tmp_backup_dir.mkdir()
    assert sqlite_repo.latest_backup(tmp_backup_dir) is None


def test_latest_backup_picks_lexically_newest(tmp_backup_dir: Path) -> None:
    tmp_backup_dir.mkdir()
    older = tmp_backup_dir / "state.db.2026-01-01"
    newer = tmp_backup_dir / "state.db.2026-04-15"
    older.write_bytes(b"older")
    newer.write_bytes(b"newer")
    assert sqlite_repo.latest_backup(tmp_backup_dir) == newer


def test_restore_raises_when_no_backup_exists(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    with pytest.raises(FileNotFoundError, match="Auto-Restore"):
        sqlite_repo.restore_from_latest_backup(tmp_db_path, tmp_backup_dir)


def test_restore_replaces_db_with_newest_backup(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    tmp_backup_dir.mkdir()
    backup = tmp_backup_dir / "state.db.2026-04-15"
    backup.write_bytes(b"BACKUP_PAYLOAD")
    tmp_db_path.write_bytes(b"OLD_PAYLOAD")
    restored_from = sqlite_repo.restore_from_latest_backup(tmp_db_path, tmp_backup_dir)
    assert restored_from == backup
    assert tmp_db_path.read_bytes() == b"BACKUP_PAYLOAD"


# --- open_state_db high-level orchestration --------------------------------


def test_open_state_db_creates_fresh_db_with_schema(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    assert not tmp_db_path.exists()
    conn = sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)
    try:
        tables = {
            row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert EXPECTED_TABLES.issubset(tables)
    finally:
        conn.close()


def test_open_state_db_opens_existing_healthy_db(tmp_db_path: Path, tmp_backup_dir: Path) -> None:
    first = sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)
    first.execute(
        "INSERT INTO projects(name, source_mode, created_at) "
        "VALUES ('foo', 'empty', '2026-01-01')"
    )
    first.close()
    second = sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)
    try:
        row = second.execute("SELECT name FROM projects").fetchone()
        assert row[0] == "foo"
    finally:
        second.close()


def test_open_state_db_restores_from_backup_on_integrity_failure(
    tmp_db_path: Path,
    tmp_backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Step 1: create a valid backup containing one project row.
    backup_conn = sqlite_repo.connect(tmp_db_path)
    sqlite_repo.apply_schema(backup_conn)
    backup_conn.execute(
        "INSERT INTO projects(name, source_mode, created_at) "
        "VALUES ('rescued', 'empty', '2026-01-01')"
    )
    backup_conn.close()
    tmp_backup_dir.mkdir()
    backup_file = tmp_backup_dir / "state.db.2026-04-15"
    backup_file.write_bytes(tmp_db_path.read_bytes())

    # Step 2: monkeypatch integrity_check to simulate corruption on the first
    # call only — the post-restore call must succeed.
    calls = {"n": 0}
    real = sqlite_repo.integrity_check

    def fake(conn: sqlite3.Connection) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            return "row 42 missing from index foo"
        return real(conn)

    monkeypatch.setattr(sqlite_repo, "integrity_check", fake)

    conn = sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)
    try:
        row = conn.execute("SELECT name FROM projects").fetchone()
        assert row[0] == "rescued"
        assert calls["n"] == 2  # one fail + one OK after restore
    finally:
        conn.close()


def test_open_state_db_raises_when_still_corrupt_after_restore(
    tmp_db_path: Path,
    tmp_backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed a backup so restore has something to copy from.
    backup_conn = sqlite_repo.connect(tmp_db_path)
    sqlite_repo.apply_schema(backup_conn)
    backup_conn.close()
    tmp_backup_dir.mkdir()
    (tmp_backup_dir / "state.db.2026-04-15").write_bytes(tmp_db_path.read_bytes())

    monkeypatch.setattr(sqlite_repo, "integrity_check", lambda _conn: "still broken")

    with pytest.raises(sqlite_repo.DatabaseIntegrityError, match="still broken"):
        sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)


def test_open_state_db_raises_with_disabled_restore(
    tmp_db_path: Path,
    tmp_backup_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir).close()  # create db
    monkeypatch.setattr(sqlite_repo, "integrity_check", lambda _conn: "broken")
    with pytest.raises(sqlite_repo.DatabaseIntegrityError, match="restore is disabled"):
        sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir, allow_restore=False)
