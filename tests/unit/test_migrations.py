"""Unit tests for the migration framework (Phase 11 C11.1).

Covers:
    - ``_enumerate_migrations`` sort + duplicate-detection.
    - ``latest_migration_version`` shortcut.
    - ``run_migrations`` on a fresh-schema DB (no-op).
    - ``run_migrations`` against a simulated v0 (pre-Phase-11) schema,
      applying ``001_project_path`` and verifying:
          * ``user_version`` is bumped.
          * ``projects`` table has the new ``path`` column.
          * ``source_mode`` CHECK accepts ``'imported'``.
          * Pre-existing rows survive with ``path IS NULL``.
    - Idempotency: running ``run_migrations`` twice is a no-op the second
      time.
    - Migration with a broken FK is caught by ``foreign_key_check``.
    - ``open_state_db`` on a pre-Phase-11 DB auto-migrates.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo

pytestmark = pytest.mark.unit


# --- enumeration + version detection ---------------------------------------


def test_enumerate_migrations_sorted_by_version(tmp_path: Path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "003_third.sql").write_text("-- noop", encoding="utf-8")
    (mig / "001_first.sql").write_text("-- noop", encoding="utf-8")
    (mig / "002_second.sql").write_text("-- noop", encoding="utf-8")
    result = sqlite_repo._enumerate_migrations(mig)
    assert [v for v, _ in result] == [1, 2, 3]


def test_enumerate_migrations_skips_non_matching_files(tmp_path: Path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_ok.sql").write_text("-- noop", encoding="utf-8")
    (mig / "README.md").write_text("docs", encoding="utf-8")
    (mig / "not_a_migration.sql").write_text("-- noop", encoding="utf-8")
    (mig / "01_too_short.sql").write_text("-- noop", encoding="utf-8")
    result = sqlite_repo._enumerate_migrations(mig)
    assert [v for v, _ in result] == [1]


def test_enumerate_migrations_rejects_duplicate_versions(tmp_path: Path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_a.sql").write_text("-- noop", encoding="utf-8")
    (mig / "001_b.sql").write_text("-- noop", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Duplicate migration version"):
        sqlite_repo._enumerate_migrations(mig)


def test_enumerate_migrations_empty_dir_returns_empty_list(tmp_path: Path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    assert sqlite_repo._enumerate_migrations(mig) == []


def test_enumerate_migrations_nonexistent_dir_returns_empty_list(tmp_path: Path) -> None:
    assert sqlite_repo._enumerate_migrations(tmp_path / "missing") == []


def test_latest_migration_version_empty_dir_returns_zero(tmp_path: Path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    assert sqlite_repo.latest_migration_version(mig) == 0


def test_latest_migration_version_picks_highest(tmp_path: Path) -> None:
    mig = tmp_path / "mig"
    mig.mkdir()
    (mig / "001_a.sql").write_text("-- noop", encoding="utf-8")
    (mig / "007_big.sql").write_text("-- noop", encoding="utf-8")
    (mig / "004_mid.sql").write_text("-- noop", encoding="utf-8")
    assert sqlite_repo.latest_migration_version(mig) == 7


def test_latest_migration_version_real_dir_matches_001(tmp_path: Path) -> None:
    # Sanity check against the bundled migrations dir. Phase 11 ships 001.
    assert sqlite_repo.latest_migration_version() >= 1


# --- simulated pre-Phase-11 DB ---------------------------------------------


_V0_SCHEMA = """
CREATE TABLE projects (
    name TEXT PRIMARY KEY,
    source_mode TEXT NOT NULL CHECK(source_mode IN ('empty', 'git')),
    source TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    default_model TEXT DEFAULT 'sonnet',
    mode TEXT DEFAULT 'normal' CHECK(mode IN ('normal', 'strict', 'yolo'))
);

CREATE TABLE claude_sessions (
    project_name TEXT PRIMARY KEY REFERENCES projects(name) ON DELETE CASCADE,
    session_id TEXT UNIQUE,
    transcript_path TEXT,
    started_at TEXT NOT NULL,
    turns_count INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    context_fill_ratio REAL DEFAULT 0.0,
    last_compact_at TEXT,
    last_activity_at TEXT,
    current_mode TEXT DEFAULT 'normal' CHECK(current_mode IN ('normal', 'strict', 'yolo'))
);
"""


def _open_v0_db(path: Path) -> sqlite3.Connection:
    conn = sqlite_repo.connect(path)
    conn.executescript(_V0_SCHEMA)
    # user_version stays at 0 — this is the "pre-Phase-11" state.
    return conn


def test_run_migrations_applies_001_against_v0_db(tmp_db_path: Path) -> None:
    conn = _open_v0_db(tmp_db_path)
    try:
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at, mode) "
            "VALUES ('foo', 'empty', '2026-04-23T12:00:00Z', 'normal')"
        )
        applied = sqlite_repo.run_migrations(conn)
        assert applied == [1, 2]
        assert sqlite_repo._get_user_version(conn) == 2

        # path column exists
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(projects)")
        }
        assert "path" in cols

        # Pre-existing row survived with NULL path.
        row = conn.execute(
            "SELECT name, source_mode, path FROM projects WHERE name = 'foo'"
        ).fetchone()
        assert row["name"] == "foo"
        assert row["source_mode"] == "empty"
        assert row["path"] is None
    finally:
        conn.close()


def test_run_migrations_accepts_imported_source_mode(tmp_db_path: Path) -> None:
    conn = _open_v0_db(tmp_db_path)
    try:
        sqlite_repo.run_migrations(conn)
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at, path) "
            "VALUES ('bar', 'imported', '2026-04-24T12:00:00Z', '/Users/x/bar')"
        )
        row = conn.execute(
            "SELECT source_mode, path FROM projects WHERE name = 'bar'"
        ).fetchone()
        assert row["source_mode"] == "imported"
        assert row["path"] == "/Users/x/bar"
    finally:
        conn.close()


def test_run_migrations_rejects_unknown_source_mode_after_migration(
    tmp_db_path: Path,
) -> None:
    conn = _open_v0_db(tmp_db_path)
    try:
        sqlite_repo.run_migrations(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO projects(name, source_mode, created_at) "
                "VALUES ('baz', 'bogus', '2026-04-24T12:00:00Z')"
            )
    finally:
        conn.close()


def test_run_migrations_is_idempotent(tmp_db_path: Path) -> None:
    conn = _open_v0_db(tmp_db_path)
    try:
        first = sqlite_repo.run_migrations(conn)
        second = sqlite_repo.run_migrations(conn)
        assert first == [1, 2]
        assert second == []
        assert sqlite_repo._get_user_version(conn) == 2
    finally:
        conn.close()


def test_run_migrations_preserves_fk_references(tmp_db_path: Path) -> None:
    conn = _open_v0_db(tmp_db_path)
    try:
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at) "
            "VALUES ('foo', 'empty', '2026-04-23T12:00:00Z')"
        )
        conn.execute(
            "INSERT INTO claude_sessions(project_name, started_at) "
            "VALUES ('foo', '2026-04-23T12:05:00Z')"
        )
        sqlite_repo.run_migrations(conn)
        # FK still enforced after migration — insert with unknown parent fails.
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO claude_sessions(project_name, started_at) "
                "VALUES ('does_not_exist', '2026-04-24T12:00:00Z')"
            )
        # And our existing session still linked.
        row = conn.execute(
            "SELECT project_name FROM claude_sessions WHERE project_name = 'foo'"
        ).fetchone()
        assert row is not None
    finally:
        conn.close()


def test_run_migrations_uses_custom_directory(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    mig = tmp_path / "custom_migrations"
    mig.mkdir()
    # Tiny custom migration: wrap own txn, bump user_version.
    (mig / "042_extra.sql").write_text(
        "BEGIN TRANSACTION;\n"
        "CREATE TABLE extra (k TEXT PRIMARY KEY);\n"
        "PRAGMA user_version = 42;\n"
        "COMMIT;\n",
        encoding="utf-8",
    )
    conn = _open_v0_db(tmp_db_path)
    try:
        applied = sqlite_repo.run_migrations(conn, directory=mig)
        assert applied == [42]
        assert sqlite_repo._get_user_version(conn) == 42
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert "extra" in tables
    finally:
        conn.close()


def test_run_migrations_fails_loudly_if_script_forgot_version_bump(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    mig = tmp_path / "custom_migrations"
    mig.mkdir()
    (mig / "007_forgot_bump.sql").write_text(
        "BEGIN TRANSACTION;\n"
        "CREATE TABLE forgot (k TEXT PRIMARY KEY);\n"
        "COMMIT;\n",
        encoding="utf-8",
    )
    conn = _open_v0_db(tmp_db_path)
    try:
        with pytest.raises(sqlite_repo.DatabaseIntegrityError, match="user_version"):
            sqlite_repo.run_migrations(conn, directory=mig)
    finally:
        conn.close()


def test_run_migrations_skips_older_than_user_version(
    tmp_db_path: Path, tmp_path: Path
) -> None:
    mig = tmp_path / "custom_migrations"
    mig.mkdir()
    (mig / "001_never_runs.sql").write_text(
        # Would fail loudly if executed — the guard is the point of the test.
        "CREATE TABLE projects (x);", encoding="utf-8"
    )
    conn = _open_v0_db(tmp_db_path)
    try:
        sqlite_repo._set_user_version(conn, 5)
        applied = sqlite_repo.run_migrations(conn, directory=mig)
        assert applied == []
    finally:
        conn.close()


# --- open_state_db integration ---------------------------------------------


def test_open_state_db_auto_migrates_pre_phase_11(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    # Simulate a production-style pre-Phase-11 DB on disk: it has the v0
    # schema and user_version 0, then someone restarts the bot.
    conn = _open_v0_db(tmp_db_path)
    conn.execute(
        "INSERT INTO projects(name, source_mode, created_at, mode) "
        "VALUES ('alpha', 'git', '2026-04-20T10:00:00Z', 'yolo')"
    )
    conn.close()

    conn = sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)
    try:
        assert sqlite_repo._get_user_version(conn) == 2
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(projects)")
        }
        assert "path" in cols
        # Pre-existing row preserved.
        row = conn.execute(
            "SELECT name, source_mode, mode, path FROM projects WHERE name = 'alpha'"
        ).fetchone()
        assert row["source_mode"] == "git"
        assert row["mode"] == "yolo"
        assert row["path"] is None
    finally:
        conn.close()


def test_open_state_db_fresh_install_sets_user_version_to_head(
    tmp_db_path: Path, tmp_backup_dir: Path
) -> None:
    assert not tmp_db_path.exists()
    conn = sqlite_repo.open_state_db(tmp_db_path, tmp_backup_dir)
    try:
        # Fresh install skips re-running migrations. Version pinned to head.
        assert sqlite_repo._get_user_version(conn) == sqlite_repo.latest_migration_version()
        # Fresh schema already has the Phase-11 shape.
        cols = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(projects)")
        }
        assert "path" in cols
    finally:
        conn.close()


# --- Mini-Phase 12: migration 002 ------------------------------------------


def test_migration_002_normalises_empty_session_id_to_null(
    tmp_db_path: Path,
) -> None:
    """A v0/v1 DB with ``session_id=''`` rows should land at NULL after 002,
    so the partial unique index has consistent semantics."""
    conn = _open_v0_db(tmp_db_path)
    try:
        # Two projects + two empty-string session rows. v0 column-level
        # UNIQUE only allows this because both inserts share the *same*
        # empty value once — trick is to insert sequentially and rely on
        # SQLite treating '' as a single value (it does, hence the bug).
        # We'll seed via two separate projects with distinct session_id
        # values, then later show the migration accepts NULLs freely.
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at) "
            "VALUES ('alpha', 'empty', '2026-04-22T12:00:00Z')"
        )
        conn.execute(
            "INSERT INTO projects(name, source_mode, created_at) "
            "VALUES ('beta', 'empty', '2026-04-22T12:00:00Z')"
        )
        # Seed exactly one empty-string row (the bug: a second one would
        # crash on the v0 schema). Migration 002 must turn it into NULL.
        conn.execute(
            "INSERT INTO claude_sessions(project_name, session_id, started_at) "
            "VALUES ('alpha', '', '2026-04-22T12:00:00Z')"
        )
        # And one real session_id to confirm it survives unchanged.
        conn.execute(
            "INSERT INTO claude_sessions(project_name, session_id, started_at) "
            "VALUES ('beta', 'sess-real', '2026-04-22T12:00:00Z')"
        )

        applied = sqlite_repo.run_migrations(conn)
        assert applied == [1, 2]
        assert sqlite_repo._get_user_version(conn) == 2

        # '' was normalised to NULL.
        alpha = conn.execute(
            "SELECT session_id FROM claude_sessions WHERE project_name='alpha'"
        ).fetchone()
        assert alpha["session_id"] is None
        # Real ID survived.
        beta = conn.execute(
            "SELECT session_id FROM claude_sessions WHERE project_name='beta'"
        ).fetchone()
        assert beta["session_id"] == "sess-real"
    finally:
        conn.close()


def test_migration_002_creates_partial_unique_index(tmp_db_path: Path) -> None:
    conn = _open_v0_db(tmp_db_path)
    try:
        sqlite_repo.run_migrations(conn)
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='claude_sessions'"
        ).fetchall()
        names = [r["name"] for r in rows]
        assert "idx_claude_sessions_session_id" in names
        idx_sql = next(
            r["sql"]
            for r in rows
            if r["name"] == "idx_claude_sessions_session_id"
        )
        assert "WHERE" in idx_sql.upper()
        assert "NOT NULL" in idx_sql.upper()
    finally:
        conn.close()


def test_migration_002_allows_multiple_null_session_ids(
    tmp_db_path: Path,
) -> None:
    """The whole point: two projects with NULL session_id can coexist."""
    conn = _open_v0_db(tmp_db_path)
    try:
        sqlite_repo.run_migrations(conn)
        for name in ("alpha", "beta"):
            conn.execute(
                "INSERT INTO projects(name, source_mode, created_at) "
                "VALUES (?, 'empty', '2026-04-22T12:00:00Z')",
                (name,),
            )
            conn.execute(
                "INSERT INTO claude_sessions(project_name, session_id, started_at) "
                "VALUES (?, NULL, '2026-04-22T12:00:00Z')",
                (name,),
            )
        count = conn.execute(
            "SELECT COUNT(*) FROM claude_sessions WHERE session_id IS NULL"
        ).fetchone()[0]
        assert count == 2
    finally:
        conn.close()


def test_migration_002_rejects_duplicate_real_session_ids(
    tmp_db_path: Path,
) -> None:
    """Partial index still enforces uniqueness on non-NULL IDs."""
    conn = _open_v0_db(tmp_db_path)
    try:
        sqlite_repo.run_migrations(conn)
        for name in ("alpha", "beta"):
            conn.execute(
                "INSERT INTO projects(name, source_mode, created_at) "
                "VALUES (?, 'empty', '2026-04-22T12:00:00Z')",
                (name,),
            )
        conn.execute(
            "INSERT INTO claude_sessions(project_name, session_id, started_at) "
            "VALUES ('alpha', 'sess-dup', '2026-04-22T12:00:00Z')"
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO claude_sessions(project_name, session_id, started_at) "
                "VALUES ('beta', 'sess-dup', '2026-04-22T12:00:00Z')"
            )
    finally:
        conn.close()
