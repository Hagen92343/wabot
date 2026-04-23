"""SQLite-backed ``ModeEventRepository`` (Spec §19 table ``mode_events``)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from whatsbot.domain.mode_events import ModeEvent, ModeEventKind
from whatsbot.domain.projects import Mode


class SqliteModeEventRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def record(self, event: ModeEvent) -> None:
        self._conn.execute(
            "INSERT INTO mode_events("
            "id, project_name, event, from_mode, to_mode, ts, msg_id"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                event.id,
                event.project_name,
                event.kind.value,
                event.from_mode.value if event.from_mode is not None else None,
                event.to_mode.value,
                int(event.at.timestamp()),
                event.msg_id,
            ),
        )

    def list_for_project(self, project_name: str) -> list[ModeEvent]:
        rows = self._conn.execute(
            "SELECT id, project_name, event, from_mode, to_mode, ts, msg_id "
            "FROM mode_events WHERE project_name = ? ORDER BY ts DESC",
            (project_name,),
        ).fetchall()
        return [_row_to_event(row) for row in rows]


def _row_to_event(row: sqlite3.Row) -> ModeEvent:
    return ModeEvent(
        id=row["id"],
        project_name=row["project_name"],
        kind=ModeEventKind(row["event"]),
        from_mode=Mode(row["from_mode"]) if row["from_mode"] else None,
        to_mode=Mode(row["to_mode"]),
        at=datetime.fromtimestamp(int(row["ts"]), tz=UTC),
        msg_id=row["msg_id"],
    )
