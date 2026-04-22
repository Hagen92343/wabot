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
    """
    path_str = ":memory:" if str(db_path) == ":memory:" else str(Path(db_path))
    if path_str != ":memory:":
        Path(path_str).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path_str, isolation_level=None)
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


def read_default_schema() -> str:
    return schema_path().read_text(encoding="utf-8")


def open_state_db(
    db_path: Path = DEFAULT_DB_PATH,
    backup_dir: Path = DEFAULT_BACKUP_DIR,
    allow_restore: bool = True,
) -> sqlite3.Connection:
    """High-level startup helper used by ``main.py``.

    1. If the DB file does not exist → create dir, connect, apply schema.
    2. Otherwise: connect, run ``PRAGMA integrity_check``.
    3. On failure: optionally restore from the newest backup and re-check;
       raise ``DatabaseIntegrityError`` if still broken.
    """
    is_memory = str(db_path) == ":memory:"
    fresh = is_memory or not Path(db_path).exists()
    conn = connect(db_path)
    if fresh:
        apply_schema(conn)
        return conn

    result = integrity_check(conn)
    if result == "ok":
        return conn

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
    return conn
