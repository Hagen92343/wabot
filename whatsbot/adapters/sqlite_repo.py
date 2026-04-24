"""SQLite-Adapter — Connection-Helper, Schema-Loader, Integrity-Check + Restore.

Spec §4: DB liegt unter
    ``~/Library/Application Support/whatsbot/state.db``
Spec §19: WAL-Mode, synchronous=NORMAL, busy_timeout=5000ms, foreign_keys=ON;
``PRAGMA integrity_check`` beim Startup, bei Fehler Auto-Restore aus dem
neuesten Backup unter ``~/Backups/whatsbot/state.db.<YYYY-MM-DD>``.

Repository-Methoden (z.B. ``upsert_project``) wandern später in eigene
Module — dieser Adapter liefert nur die Infrastruktur.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from pathlib import Path
from typing import Final

DEFAULT_DB_PATH: Final[Path] = (
    Path.home() / "Library" / "Application Support" / "whatsbot" / "state.db"
)
DEFAULT_BACKUP_DIR: Final[Path] = Path.home() / "Backups" / "whatsbot"

PRAGMAS: Final[tuple[str, ...]] = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=5000",
    "PRAGMA foreign_keys=ON",
)


class DatabaseIntegrityError(RuntimeError):
    """``PRAGMA integrity_check`` returned anything other than ``ok``."""


def connect(db_path: Path | str = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """Open a SQLite connection with the four whatsbot-PRAGMAs applied.

    ``isolation_level=None`` puts the connection in autocommit so PRAGMA
    statements take effect immediately. Higher layers will wrap multi-step
    operations in explicit ``BEGIN``/``COMMIT``.

    ``check_same_thread=False`` is required because FastAPI dispatches
    handlers on a worker thread while the connection is opened on the
    startup thread. SQLite itself is thread-safe (Python's sqlite3
    module is built in serialized mode); the ``check_same_thread``
    flag is only a Python-level guard. Single-user, single-connection
    serial use — no concurrent writers — so no race.
    """
    path_str = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path))
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(
        path_str, isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    for stmt in PRAGMAS:
        conn.execute(stmt)
    return conn


def apply_schema(conn: sqlite3.Connection, schema_sql: str | None = None) -> None:
    """Apply ``sql/schema.sql`` (or an override) to the connection.

    The bundled schema uses bare ``CREATE TABLE`` (no ``IF NOT EXISTS``) — this
    function is safe to call only on a freshly created DB.
    """
    if schema_sql is None:
        schema_sql = read_default_schema()
    conn.executescript(schema_sql)


def integrity_check(conn: sqlite3.Connection) -> str:
    """Return the first row of ``PRAGMA integrity_check`` (``"ok"`` on healthy DB)."""
    cur = conn.execute("PRAGMA integrity_check")
    row = cur.fetchone()
    if row is None:
        return "no result"
    # row is sqlite3.Row → indexable by position
    return str(row[0])


def latest_backup(backup_dir: Path = DEFAULT_BACKUP_DIR) -> Path | None:
    """Return the newest ``state.db.<date>`` file in ``backup_dir``, or ``None``."""
    if not backup_dir.is_dir():
        return None
    backups = sorted(backup_dir.glob("state.db.*"))
    return backups[-1] if backups else None


def restore_from_latest_backup(
    db_path: Path = DEFAULT_DB_PATH,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
) -> Path:
    """Replace ``db_path`` with the newest backup file. Returns its path.

    Raises ``FileNotFoundError`` if no backup is available — Startup must
    treat that as a fatal condition.
    """
    backup = latest_backup(backup_dir)
    if backup is None:
        raise FileNotFoundError(
            f"Kein Backup unter {backup_dir} gefunden — Auto-Restore unmöglich."
        )
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Best-effort cleanup of WAL/SHM siblings before restore.
    for sibling in (
        db_path,
        db_path.with_suffix(db_path.suffix + "-wal"),
        db_path.with_suffix(db_path.suffix + "-shm"),
    ):
        sibling.unlink(missing_ok=True)
    shutil.copy2(backup, db_path)
    return backup


def schema_path() -> Path:
    """Resolve to ``<repo>/sql/schema.sql`` independent of cwd."""
    # whatsbot/adapters/sqlite_repo.py -> repo root -> sql/schema.sql
    return Path(__file__).resolve().parents[2] / "sql" / "schema.sql"


def migrations_dir() -> Path:
    """Resolve to ``<repo>/sql/migrations/`` independent of cwd."""
    return Path(__file__).resolve().parents[2] / "sql" / "migrations"


def read_default_schema() -> str:
    return schema_path().read_text(encoding="utf-8")


_MIGRATION_NAME_RE: Final = re.compile(r"^(\d{3,})_.+\.sql$")


def _enumerate_migrations(
    directory: Path | None = None,
) -> list[tuple[int, Path]]:
    """Return sorted (version, path) for every migration under ``directory``.

    Numbering is zero-padded ``NNN_description.sql`` (e.g. ``001_project_path.sql``).
    Versions must be unique; duplicates raise ``RuntimeError``.
    """
    target = directory or migrations_dir()
    if not target.is_dir():
        return []
    found: dict[int, Path] = {}
    for entry in target.iterdir():
        if not entry.is_file():
            continue
        match = _MIGRATION_NAME_RE.match(entry.name)
        if not match:
            continue
        version = int(match.group(1))
        if version in found:
            raise RuntimeError(
                f"Duplicate migration version {version}: "
                f"{found[version].name} vs {entry.name}"
            )
        found[version] = entry
    return sorted(found.items())


def _get_user_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("PRAGMA user_version").fetchone()
    return int(row[0]) if row is not None else 0


def _set_user_version(conn: sqlite3.Connection, version: int) -> None:
    # PRAGMA doesn't support ? parameter binding — integer interpolation is
    # safe here because ``version`` came from a regex-matched filename.
    conn.execute(f"PRAGMA user_version = {int(version)}")


def latest_migration_version(directory: Path | None = None) -> int:
    """Return the highest migration version on disk, or 0 if none."""
    entries = _enumerate_migrations(directory)
    return entries[-1][0] if entries else 0


def run_migrations(
    conn: sqlite3.Connection,
    directory: Path | None = None,
) -> list[int]:
    """Apply all pending migrations in order. Return applied version list.

    Design notes:
        - ``PRAGMA user_version`` is the source of truth for "what's applied".
          Each migration script bumps it itself (inside the script's own
          transaction), so a mid-migration crash leaves v(N-1) intact.
        - ``PRAGMA foreign_keys`` is toggled OFF around each migration
          (required for the rename-copy-drop table-rebuild pattern, and
          it can't be changed inside a transaction in autocommit mode).
        - Python ``executescript()`` handles its own transaction control;
          the script therefore wraps itself in ``BEGIN;…COMMIT;``. On error
          in the middle, SQLite rolls the txn back automatically.
        - After each migration, ``PRAGMA foreign_key_check`` runs as a
          safety net — any dangling references abort the chain.
    """
    current = _get_user_version(conn)
    applied: list[int] = []
    for version, path in _enumerate_migrations(directory):
        if version <= current:
            continue
        script = path.read_text(encoding="utf-8")
        conn.execute("PRAGMA foreign_keys = OFF")
        try:
            conn.executescript(script)
            fk_issues = conn.execute("PRAGMA foreign_key_check").fetchall()
            if fk_issues:
                raise DatabaseIntegrityError(
                    f"Migration {version} left dangling FKs: {fk_issues!r}"
                )
            new_version = _get_user_version(conn)
            if new_version != version:
                raise DatabaseIntegrityError(
                    f"Migration {version} did not bump user_version "
                    f"(got {new_version!r}). Script must end with "
                    f"``PRAGMA user_version = {version};`` inside the txn."
                )
        finally:
            conn.execute("PRAGMA foreign_keys = ON")
        applied.append(version)
    return applied


def open_state_db(
    db_path: Path = DEFAULT_DB_PATH,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    allow_restore: bool = True,
) -> sqlite3.Connection:
    """High-level startup helper used by ``main.py``.

    1. If the DB file does not exist → create dir, connect, apply schema,
       pin ``user_version`` to the latest migration (fresh installs don't
       need to re-run migrations against a schema that's already at head).
    2. Otherwise: connect, run ``PRAGMA integrity_check``, then apply any
       pending migrations via ``run_migrations``.
    3. On integrity failure: optionally restore from the newest backup and
       re-check; raise ``DatabaseIntegrityError`` if still broken. Pending
       migrations still run after a successful restore.
    """
    is_memory = str(db_path) == ":memory:"
    fresh = is_memory or not Path(db_path).exists()
    conn = connect(db_path)
    if fresh:
        apply_schema(conn)
        _set_user_version(conn, latest_migration_version())
        return conn

    result = integrity_check(conn)
    if result != "ok":
        conn.close()
        if not allow_restore:
            raise DatabaseIntegrityError(
                f"integrity_check returned {result!r} and restore is disabled."
            )
        restored_from = restore_from_latest_backup(Path(db_path), backup_dir)
        conn = connect(db_path)
        result = integrity_check(conn)
        if result != "ok":
            conn.close()
            raise DatabaseIntegrityError(
                f"DB still corrupt after restore from {restored_from}: {result!r}"
            )

    # Existing DB (possibly just-restored): apply any pending migrations.
    run_migrations(conn)
    return conn
