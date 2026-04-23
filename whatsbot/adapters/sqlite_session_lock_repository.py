"""SQLite-backed SessionLockRepository (Spec §19 ``session_locks`` table)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from whatsbot.domain.locks import LockOwner, SessionLock


class SqliteSessionLockRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, project_name: str) -> SessionLock | None:
        row = self._conn.execute(
            "SELECT project_name, owner, acquired_at, last_activity_at "
            "FROM session_locks WHERE project_name = ?",
            (project_name,),
        ).fetchone()
        return _row_to_lock(row) if row is not None else None

    def upsert(self, lock: SessionLock) -> None:
        self._conn.execute(
            "INSERT INTO session_locks("
            "project_name, owner, acquired_at, last_activity_at"
            ") VALUES (?, ?, ?, ?) "
            "ON CONFLICT(project_name) DO UPDATE SET "
            "owner = excluded.owner, "
            "acquired_at = excluded.acquired_at, "
            "last_activity_at = excluded.last_activity_at",
            (
                lock.project_name,
                lock.owner.value,
                int(lock.acquired_at.timestamp()),
                int(lock.last_activity_at.timestamp()),
            ),
        )

    def delete(self, project_name: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM session_locks WHERE project_name = ?",
            (project_name,),
        )
        return cursor.rowcount > 0

    def list_all(self) -> list[SessionLock]:
        rows = self._conn.execute(
            "SELECT project_name, owner, acquired_at, last_activity_at "
            "FROM session_locks ORDER BY project_name"
        ).fetchall()
        return [_row_to_lock(row) for row in rows]


def _row_to_lock(row: sqlite3.Row) -> SessionLock:
    return SessionLock(
        project_name=row["project_name"],
        owner=LockOwner(row["owner"]),
        acquired_at=datetime.fromtimestamp(int(row["acquired_at"]), tz=UTC),
        last_activity_at=datetime.fromtimestamp(
            int(row["last_activity_at"]), tz=UTC
        ),
    )
