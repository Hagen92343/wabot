"""SQLite-backed AllowRuleRepository (Spec §19 ``allow_rules`` table)."""

from __future__ import annotations

import sqlite3
from datetime import datetime

from whatsbot.domain.allow_rules import AllowRulePattern, AllowRuleSource
from whatsbot.ports.allow_rule_repository import StoredAllowRule


def _row_to_rule(row: sqlite3.Row) -> StoredAllowRule:
    return StoredAllowRule(
        id=row["id"],
        project_name=row["project_name"],
        pattern=AllowRulePattern(tool=row["tool"], pattern=row["pattern"]),
        source=AllowRuleSource(row["source"]),
        created_at=datetime.fromisoformat(row["created_at"]),
    )


class SqliteAllowRuleRepository:
    """Concrete repository against an open ``sqlite3.Connection``."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    def add(
        self,
        project_name: str,
        pattern: AllowRulePattern,
        source: AllowRuleSource,
    ) -> StoredAllowRule:
        # Idempotent: if (project + tool + pattern) already exists, return
        # the existing row instead of inserting a duplicate. This lets the
        # caller (`AllowService.batch_approve`) be replayed safely.
        existing = self._conn.execute(
            "SELECT * FROM allow_rules "
            "WHERE project_name = ? AND tool = ? AND pattern = ? "
            "LIMIT 1",
            (project_name, pattern.tool, pattern.pattern),
        ).fetchone()
        if existing is not None:
            return _row_to_rule(existing)

        created_at = datetime.now().astimezone()
        cursor = self._conn.execute(
            "INSERT INTO allow_rules"
            "(project_name, tool, pattern, created_at, source) "
            "VALUES (?, ?, ?, ?, ?)",
            (
                project_name,
                pattern.tool,
                pattern.pattern,
                created_at.isoformat(),
                source.value,
            ),
        )
        new_id = cursor.lastrowid
        assert new_id is not None  # SQLite returns an integer for AUTOINCREMENT
        return StoredAllowRule(
            id=int(new_id),
            project_name=project_name,
            pattern=pattern,
            source=source,
            created_at=created_at,
        )

    def remove(self, project_name: str, pattern: AllowRulePattern) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM allow_rules " "WHERE project_name = ? AND tool = ? AND pattern = ?",
            (project_name, pattern.tool, pattern.pattern),
        )
        return cursor.rowcount > 0

    def list_for_project(self, project_name: str) -> list[StoredAllowRule]:
        rows = self._conn.execute(
            "SELECT * FROM allow_rules WHERE project_name = ? ORDER BY id",
            (project_name,),
        ).fetchall()
        return [_row_to_rule(r) for r in rows]
