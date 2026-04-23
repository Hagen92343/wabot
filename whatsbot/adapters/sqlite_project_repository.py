"""SQLite-backed ProjectRepository.

Maps the ``projects`` table from Spec §19 to ``Project`` dataclasses.
Datetimes are stored as ISO-8601 strings to keep the rows human-readable
when poking at the DB with ``sqlite3 ".schema"``.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime

from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.project_repository import (
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
)


def _row_to_project(row: sqlite3.Row) -> Project:
    return Project(
        name=row["name"],
        source_mode=SourceMode(row["source_mode"]),
        source=row["source"],
        created_at=datetime.fromisoformat(row["created_at"]),
        last_used_at=(datetime.fromisoformat(row["last_used_at"]) if row["last_used_at"] else None),
        default_model=row["default_model"] or "sonnet",
        mode=Mode(row["mode"] or "normal"),
    )


class SqliteProjectRepository:
    """Concrete ``ProjectRepository`` against an open ``sqlite3.Connection``.

    The connection is provided at construction so we don't bind a connection
    pool here — Phase 1's ``open_state_db`` returns one connection per
    process, which is fine for the single-user bot.
    """

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def create(self, project: Project) -> None:
        try:
            self._conn.execute(
                """
                INSERT INTO projects(
                    name, source_mode, source,
                    created_at, last_used_at,
                    default_model, mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project.name,
                    project.source_mode.value,
                    project.source,
                    project.created_at.isoformat(),
                    project.last_used_at.isoformat() if project.last_used_at else None,
                    project.default_model,
                    project.mode.value,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # Either the PRIMARY KEY (name) collided or a CHECK constraint
            # tripped. Disambiguate by looking up the row.
            if self.exists(project.name):
                raise ProjectAlreadyExistsError(
                    f"Projekt '{project.name}' existiert schon."
                ) from exc
            raise

    def get(self, name: str) -> Project:
        row = self._conn.execute("SELECT * FROM projects WHERE name = ?", (name,)).fetchone()
        if row is None:
            raise ProjectNotFoundError(f"Projekt '{name}' nicht gefunden.")
        return _row_to_project(row)

    def list_all(self) -> list[Project]:
        rows = self._conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        return [_row_to_project(r) for r in rows]

    def delete(self, name: str) -> None:
        cur = self._conn.execute("DELETE FROM projects WHERE name = ?", (name,))
        if cur.rowcount == 0:
            raise ProjectNotFoundError(f"Projekt '{name}' nicht gefunden.")

    def exists(self, name: str) -> bool:
        row = self._conn.execute("SELECT 1 FROM projects WHERE name = ?", (name,)).fetchone()
        return row is not None

    def update_mode(self, name: str, mode: Mode) -> None:
        cursor = self._conn.execute(
            "UPDATE projects SET mode = ? WHERE name = ?",
            (mode.value, name),
        )
        if cursor.rowcount == 0:
            raise ProjectNotFoundError(f"Projekt '{name}' nicht gefunden.")
