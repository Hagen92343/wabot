"""SQLite-backed PendingOutputRepository (Spec §19)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from whatsbot.domain.pending_outputs import PendingOutput


class SqlitePendingOutputRepository:
    """Concrete repository against an open ``sqlite3.Connection``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, output: PendingOutput) -> None:
        self._conn.execute(
            "INSERT INTO pending_outputs("
            "msg_id, project_name, output_path, size_bytes, created_at, deadline_ts"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                output.msg_id,
                output.project_name,
                output.output_path,
                output.size_bytes,
                _iso(output.created_at),
                output.deadline_ts,
            ),
        )

    def get(self, msg_id: str) -> PendingOutput | None:
        row = self._conn.execute(
            "SELECT msg_id, project_name, output_path, size_bytes, created_at, deadline_ts "
            "FROM pending_outputs WHERE msg_id = ?",
            (msg_id,),
        ).fetchone()
        return _row_to_output(row) if row is not None else None

    def latest_open(self) -> PendingOutput | None:
        row = self._conn.execute(
            "SELECT msg_id, project_name, output_path, size_bytes, created_at, deadline_ts "
            "FROM pending_outputs ORDER BY created_at DESC, msg_id DESC LIMIT 1"
        ).fetchone()
        return _row_to_output(row) if row is not None else None

    def resolve(self, msg_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM pending_outputs WHERE msg_id = ?",
            (msg_id,),
        )
        return cursor.rowcount > 0

    def delete_expired(self, now_ts: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT msg_id FROM pending_outputs WHERE deadline_ts <= ?",
            (now_ts,),
        ).fetchall()
        expired = [row["msg_id"] for row in rows]
        if expired:
            self._conn.execute(
                "DELETE FROM pending_outputs WHERE deadline_ts <= ?",
                (now_ts,),
            )
        return expired


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _row_to_output(row: sqlite3.Row) -> PendingOutput:
    return PendingOutput(
        msg_id=row["msg_id"],
        project_name=row["project_name"],
        output_path=row["output_path"],
        size_bytes=int(row["size_bytes"]),
        created_at=_parse_iso(row["created_at"]),
        deadline_ts=int(row["deadline_ts"]),
    )


def _parse_iso(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)
