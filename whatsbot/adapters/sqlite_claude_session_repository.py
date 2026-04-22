"""SQLite-backed ClaudeSessionRepository (Spec §19)."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import ClaudeSession, context_fill_ratio


class SqliteClaudeSessionRepository:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn

    # ---- CRUD --------------------------------------------------------

    def upsert(self, session: ClaudeSession) -> None:
        self._conn.execute(
            "INSERT INTO claude_sessions("
            "project_name, session_id, transcript_path, started_at, "
            "turns_count, tokens_used, context_fill_ratio, "
            "last_compact_at, last_activity_at, current_mode) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(project_name) DO UPDATE SET "
            "session_id = excluded.session_id, "
            "transcript_path = excluded.transcript_path, "
            "started_at = excluded.started_at, "
            "turns_count = excluded.turns_count, "
            "tokens_used = excluded.tokens_used, "
            "context_fill_ratio = excluded.context_fill_ratio, "
            "last_compact_at = excluded.last_compact_at, "
            "last_activity_at = excluded.last_activity_at, "
            "current_mode = excluded.current_mode",
            (
                session.project_name,
                session.session_id,
                session.transcript_path,
                _iso(session.started_at),
                session.turns_count,
                session.tokens_used,
                session.context_fill_ratio,
                _iso_or_none(session.last_compact_at),
                _iso_or_none(session.last_activity_at),
                session.current_mode.value,
            ),
        )

    def get(self, project_name: str) -> ClaudeSession | None:
        row = self._conn.execute(
            "SELECT project_name, session_id, transcript_path, started_at, "
            "turns_count, tokens_used, context_fill_ratio, "
            "last_compact_at, last_activity_at, current_mode "
            "FROM claude_sessions WHERE project_name = ?",
            (project_name,),
        ).fetchone()
        return _row_to_session(row) if row is not None else None

    def list_all(self) -> list[ClaudeSession]:
        rows = self._conn.execute(
            "SELECT project_name, session_id, transcript_path, started_at, "
            "turns_count, tokens_used, context_fill_ratio, "
            "last_compact_at, last_activity_at, current_mode "
            "FROM claude_sessions ORDER BY project_name ASC"
        ).fetchall()
        return [_row_to_session(r) for r in rows]

    def delete(self, project_name: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM claude_sessions WHERE project_name = ?",
            (project_name,),
        )
        return cursor.rowcount > 0

    # ---- Hot-path partial updates -----------------------------------

    def update_activity(
        self,
        project_name: str,
        *,
        tokens_used: int,
        last_activity_at: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE claude_sessions SET "
            "tokens_used = ?, context_fill_ratio = ?, last_activity_at = ? "
            "WHERE project_name = ?",
            (
                tokens_used,
                context_fill_ratio(tokens_used),
                _iso(last_activity_at),
                project_name,
            ),
        )

    def bump_turn(self, project_name: str, *, at: datetime) -> None:
        self._conn.execute(
            "UPDATE claude_sessions SET "
            "turns_count = turns_count + 1, last_activity_at = ? "
            "WHERE project_name = ?",
            (_iso(at), project_name),
        )

    def update_mode(self, project_name: str, mode: Mode) -> None:
        self._conn.execute(
            "UPDATE claude_sessions SET current_mode = ? WHERE project_name = ?",
            (mode.value, project_name),
        )

    def mark_compact(self, project_name: str, at: datetime) -> None:
        self._conn.execute(
            "UPDATE claude_sessions SET "
            "last_compact_at = ?, tokens_used = 0, context_fill_ratio = 0.0 "
            "WHERE project_name = ?",
            (_iso(at), project_name),
        )


# ---- (De)serialisation helpers ----------------------------------------


def _iso(ts: datetime) -> str:
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    return ts.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _iso_or_none(ts: datetime | None) -> str | None:
    return _iso(ts) if ts is not None else None


def _parse_iso(raw: str) -> datetime:
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    return datetime.fromisoformat(raw)


def _parse_iso_or_none(raw: str | None) -> datetime | None:
    return _parse_iso(raw) if raw else None


def _row_to_session(row: sqlite3.Row) -> ClaudeSession:
    return ClaudeSession(
        project_name=row["project_name"],
        session_id=row["session_id"] or "",
        transcript_path=row["transcript_path"] or "",
        started_at=_parse_iso(row["started_at"]),
        turns_count=int(row["turns_count"] or 0),
        tokens_used=int(row["tokens_used"] or 0),
        context_fill_ratio=float(row["context_fill_ratio"] or 0.0),
        last_compact_at=_parse_iso_or_none(row["last_compact_at"]),
        last_activity_at=_parse_iso_or_none(row["last_activity_at"]),
        current_mode=Mode(row["current_mode"] or "normal"),
    )
