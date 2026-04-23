"""SQLite-backed MaxLimitsRepository (Spec §19 ``max_limits`` table).

Mirrors the shape of the other Phase 1-7 SQLite adapters. Everything
on a single open connection; the application layer owns lifecycle.
"""

from __future__ import annotations

import sqlite3

from whatsbot.domain.limits import LimitKind, MaxLimit


class SqliteMaxLimitsRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def get(self, kind: LimitKind) -> MaxLimit | None:
        row = self._conn.execute(
            "SELECT kind, reset_at_ts, warned_at_ts, remaining_pct "
            "FROM max_limits WHERE kind = ?",
            (kind.value,),
        ).fetchone()
        return _row_to_limit(row) if row is not None else None

    def upsert(self, limit: MaxLimit) -> None:
        self._conn.execute(
            "INSERT INTO max_limits("
            "kind, reset_at_ts, warned_at_ts, remaining_pct"
            ") VALUES (?, ?, ?, ?) "
            "ON CONFLICT(kind) DO UPDATE SET "
            "reset_at_ts = excluded.reset_at_ts, "
            "warned_at_ts = excluded.warned_at_ts, "
            "remaining_pct = excluded.remaining_pct",
            (
                limit.kind.value,
                int(limit.reset_at_ts),
                limit.warned_at_ts,
                float(limit.remaining_pct),
            ),
        )

    def delete(self, kind: LimitKind) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM max_limits WHERE kind = ?",
            (kind.value,),
        )
        return cursor.rowcount > 0

    def list_all(self) -> list[MaxLimit]:
        rows = self._conn.execute(
            "SELECT kind, reset_at_ts, warned_at_ts, remaining_pct "
            "FROM max_limits ORDER BY reset_at_ts ASC"
        ).fetchall()
        return [_row_to_limit(row) for row in rows]

    def mark_warned(
        self, kind: LimitKind, warned_at_ts: int
    ) -> None:
        self._conn.execute(
            "UPDATE max_limits SET warned_at_ts = ? WHERE kind = ?",
            (int(warned_at_ts), kind.value),
        )


def _row_to_limit(row: sqlite3.Row) -> MaxLimit:
    warned_raw = row["warned_at_ts"]
    warned: int | None = (
        int(warned_raw) if warned_raw is not None else None
    )
    remaining_raw = row["remaining_pct"]
    remaining = (
        float(remaining_raw) if remaining_raw is not None else -1.0
    )
    return MaxLimit(
        kind=LimitKind(row["kind"]),
        reset_at_ts=int(row["reset_at_ts"]),
        warned_at_ts=warned,
        remaining_pct=remaining,
    )
