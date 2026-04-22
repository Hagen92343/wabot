"""SQLite-backed AppStateRepository (Spec §19 ``app_state`` table)."""

from __future__ import annotations

import sqlite3


class SqliteAppStateRepository:
    """Concrete repository against an open ``sqlite3.Connection``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, key: str) -> str | None:
        row = self._conn.execute("SELECT value FROM app_state WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        value = row["value"]
        return None if value is None else str(value)

    def set(self, key: str, value: str) -> None:
        # Spec §19 has app_state(key PRIMARY KEY, value) — UPSERT keeps it
        # idempotent across replays without needing a separate has-key
        # check from the caller.
        self._conn.execute(
            "INSERT INTO app_state(key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    def delete(self, key: str) -> bool:
        cursor = self._conn.execute("DELETE FROM app_state WHERE key = ?", (key,))
        return cursor.rowcount > 0
