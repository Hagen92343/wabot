"""SQLite-backed PendingConfirmationRepository (Spec §19)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from whatsbot.domain.pending_confirmations import (
    ConfirmationKind,
    PendingConfirmation,
)


class SqlitePendingConfirmationRepository:
    """Concrete repository against an open ``sqlite3.Connection``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, confirmation: PendingConfirmation) -> None:
        # created_at as ISO-8601 UTC — matches the TEXT column convention
        # used elsewhere (projects.created_at, allow_rules.created_at).
        self._conn.execute(
            "INSERT INTO pending_confirmations(id, project_name, kind, payload, "
            "deadline_ts, created_at, msg_id) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                confirmation.id,
                confirmation.project_name,
                confirmation.kind.value,
                confirmation.payload,
                confirmation.deadline_ts,
                _iso(confirmation.created_at),
                confirmation.msg_id,
            ),
        )

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        row = self._conn.execute(
            "SELECT id, project_name, kind, payload, deadline_ts, created_at, msg_id "
            "FROM pending_confirmations WHERE id = ?",
            (confirmation_id,),
        ).fetchone()
        return _row_to_confirmation(row) if row is not None else None

    def resolve(self, confirmation_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM pending_confirmations WHERE id = ?",
            (confirmation_id,),
        )
        return cursor.rowcount > 0

    def list_open(self) -> list[PendingConfirmation]:
        rows = self._conn.execute(
            "SELECT id, project_name, kind, payload, deadline_ts, created_at, msg_id "
            "FROM pending_confirmations ORDER BY created_at ASC, id ASC"
        ).fetchall()
        return [_row_to_confirmation(r) for r in rows]

    def delete_expired(self, now_ts: int) -> list[str]:
        rows = self._conn.execute(
            "SELECT id FROM pending_confirmations WHERE deadline_ts <= ?",
            (now_ts,),
        ).fetchall()
        expired = [row["id"] for row in rows]
        if expired:
            self._conn.execute(
                "DELETE FROM pending_confirmations WHERE deadline_ts <= ?",
                (now_ts,),
            )
        return expired


def _iso(ts: datetime) -> str:
    """Format a UTC datetime as ISO-8601 with a trailing 'Z'."""
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _row_to_confirmation(row: sqlite3.Row) -> PendingConfirmation:
    return PendingConfirmation(
        id=row["id"],
        project_name=row["project_name"],
        kind=ConfirmationKind(row["kind"]),
        payload=row["payload"],
        deadline_ts=int(row["deadline_ts"]),
        created_at=_parse_iso(row["created_at"]),
        msg_id=row["msg_id"],
    )


def _parse_iso(raw: str) -> datetime:
    """Parse the ISO-8601 form we emit — trailing 'Z' included."""
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)
