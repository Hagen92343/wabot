"""SQLite-backed PendingDeleteRepository (Spec §19 ``pending_deletes`` table)."""

from __future__ import annotations

import sqlite3

from whatsbot.domain.pending_deletes import PendingDelete


class SqlitePendingDeleteRepository:
    """Concrete repository against an open ``sqlite3.Connection``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def upsert(self, pending: PendingDelete) -> None:
        self._conn.execute(
            "INSERT INTO pending_deletes(project_name, deadline_ts) "
            "VALUES (?, ?) "
            "ON CONFLICT(project_name) DO UPDATE SET deadline_ts = excluded.deadline_ts",
            (pending.project_name, pending.deadline_ts),
        )

    def get(self, project_name: str) -> PendingDelete | None:
        row = self._conn.execute(
            "SELECT project_name, deadline_ts FROM pending_deletes WHERE project_name = ?",
            (project_name,),
        ).fetchone()
        if row is None:
            return None
        return PendingDelete(
            project_name=row["project_name"],
            deadline_ts=int(row["deadline_ts"]),
        )

    def delete(self, project_name: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM pending_deletes WHERE project_name = ?",
            (project_name,),
        )
        return cursor.rowcount > 0

    def delete_expired(self, now_ts: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT project_name FROM pending_deletes WHERE deadline_ts <= ?",
            (now_ts,),
        ).fetchall()
        expired = [row["project_name"] for row in rows]
        if expired:
            self._conn.execute(
                "DELETE FROM pending_deletes WHERE deadline_ts <= ?",
                (now_ts,),
            )
        return expired
