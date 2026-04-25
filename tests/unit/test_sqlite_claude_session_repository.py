"""Unit tests for SqliteClaudeSessionRepository."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession

pytestmark = pytest.mark.unit


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    # claude_sessions has an FK to projects — seed one so inserts pass.
    SqliteProjectRepository(c).create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def repo(conn: sqlite3.Connection) -> SqliteClaudeSessionRepository:
    return SqliteClaudeSessionRepository(conn)


def _sample(
    project: str = "alpha",
    mode: Mode = Mode.NORMAL,
    session_id: str = "sess-01",
) -> ClaudeSession:
    return ClaudeSession(
        project_name=project,
        session_id=session_id,
        transcript_path="/tmp/t.jsonl",
        started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        current_mode=mode,
    )


# ---- upsert + get -----------------------------------------------------


class TestUpsertGet:
    def test_missing_returns_none(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        assert repo.get("ghost") is None

    def test_roundtrip(self, repo: SqliteClaudeSessionRepository) -> None:
        s = _sample()
        repo.upsert(s)
        fetched = repo.get(s.project_name)
        assert fetched == s

    def test_upsert_overwrites(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        repo.upsert(_sample(session_id="sess-01"))
        repo.upsert(_sample(session_id="sess-02"))
        got = repo.get("alpha")
        assert got is not None
        assert got.session_id == "sess-02"

    def test_mode_roundtrip_for_each_mode(
        self, conn: sqlite3.Connection, repo: SqliteClaudeSessionRepository
    ) -> None:
        # Add more projects so FK passes for each mode variant.
        # Distinct non-empty session_ids — the partial unique index
        # (Mini-Phase 12) enforces uniqueness only on non-NULL values.
        project_repo = SqliteProjectRepository(conn)
        for name, mode, sid in [
            ("beta", Mode.STRICT, "sess-beta"),
            ("gamma", Mode.YOLO, "sess-gamma"),
        ]:
            project_repo.create(
                Project(
                    name=name,
                    source_mode=SourceMode.EMPTY,
                    created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
                    mode=mode,
                )
            )
            repo.upsert(_sample(project=name, mode=mode, session_id=sid))
            got = repo.get(name)
            assert got is not None
            assert got.current_mode is mode


# ---- list_all + delete -----------------------------------------------


def test_list_all_empty(repo: SqliteClaudeSessionRepository) -> None:
    assert repo.list_all() == []


def test_list_all_orders_by_project_name(
    conn: sqlite3.Connection, repo: SqliteClaudeSessionRepository
) -> None:
    project_repo = SqliteProjectRepository(conn)
    for name in ("zeta", "beta"):
        project_repo.create(
            Project(
                name=name,
                source_mode=SourceMode.EMPTY,
                created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
                mode=Mode.NORMAL,
            )
        )
    # session_id is UNIQUE — distinct IDs per project.
    for name in ("zeta", "alpha", "beta"):
        repo.upsert(_sample(project=name, session_id=f"sess-{name}"))
    names = [s.project_name for s in repo.list_all()]
    assert names == ["alpha", "beta", "zeta"]


def test_delete_returns_true_when_present(
    repo: SqliteClaudeSessionRepository,
) -> None:
    repo.upsert(_sample())
    assert repo.delete("alpha") is True
    assert repo.get("alpha") is None


def test_delete_returns_false_when_absent(
    repo: SqliteClaudeSessionRepository,
) -> None:
    assert repo.delete("alpha") is False


# ---- hot-path partial updates ----------------------------------------


class TestPartialUpdates:
    def test_update_activity_touches_only_tokens_and_activity(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        s = _sample()
        repo.upsert(s)
        at = datetime(2026, 4, 22, 12, 30, tzinfo=UTC)
        repo.update_activity(
            "alpha", tokens_used=160_000, last_activity_at=at
        )
        got = repo.get("alpha")
        assert got is not None
        assert got.tokens_used == 160_000
        assert got.context_fill_ratio == pytest.approx(160_000 / 200_000)
        assert got.last_activity_at == at
        # turns_count untouched.
        assert got.turns_count == s.turns_count

    def test_bump_turn_increments_and_refreshes(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        repo.upsert(_sample())
        at = datetime(2026, 4, 22, 12, 31, tzinfo=UTC)
        repo.bump_turn("alpha", at=at)
        repo.bump_turn("alpha", at=at)
        got = repo.get("alpha")
        assert got is not None
        assert got.turns_count == 2
        assert got.last_activity_at == at

    def test_update_mode_only_touches_mode(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        repo.upsert(_sample(mode=Mode.NORMAL))
        repo.update_mode("alpha", Mode.STRICT)
        got = repo.get("alpha")
        assert got is not None
        assert got.current_mode is Mode.STRICT

    def test_mark_compact_resets_tokens(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        # Pre-load a filled-up session.
        s = _sample()
        repo.upsert(s)
        repo.update_activity(
            "alpha",
            tokens_used=180_000,
            last_activity_at=datetime(2026, 4, 22, 12, 45, tzinfo=UTC),
        )
        at = datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
        repo.mark_compact("alpha", at)
        got = repo.get("alpha")
        assert got is not None
        assert got.tokens_used == 0
        assert got.context_fill_ratio == 0.0
        assert got.last_compact_at == at


# ---- FK cascade behaviour --------------------------------------------


def test_deleting_project_cascades_to_claude_session(
    conn: sqlite3.Connection, repo: SqliteClaudeSessionRepository
) -> None:
    """Spec §19 has ``ON DELETE CASCADE`` on the claude_sessions FK.
    Verifies it's actually wired through our connection (foreign_keys=ON
    pragma)."""
    repo.upsert(_sample())
    SqliteProjectRepository(conn).delete("alpha")
    assert repo.get("alpha") is None


# ---- Mini-Phase 12: partial unique index on session_id ---------------


class TestSessionIdPartialUnique:
    """Empty session_id is the placeholder for "Claude has not produced
    an ID yet". Two such rows used to crash on the column-level UNIQUE
    constraint — Mini-Phase 12 replaced it with a partial unique index
    and normalises empty strings to NULL on insert."""

    def _seed_extra_project(
        self, conn: sqlite3.Connection, name: str
    ) -> None:
        SqliteProjectRepository(conn).create(
            Project(
                name=name,
                source_mode=SourceMode.EMPTY,
                created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
                mode=Mode.NORMAL,
            )
        )

    def test_two_empty_session_ids_can_coexist(
        self, conn: sqlite3.Connection, repo: SqliteClaudeSessionRepository
    ) -> None:
        self._seed_extra_project(conn, "beta")
        repo.upsert(_sample(project="alpha", session_id=""))
        repo.upsert(_sample(project="beta", session_id=""))
        assert repo.get("alpha") is not None
        assert repo.get("beta") is not None

    def test_empty_session_id_persists_as_null(
        self, conn: sqlite3.Connection, repo: SqliteClaudeSessionRepository
    ) -> None:
        repo.upsert(_sample(session_id=""))
        raw = conn.execute(
            "SELECT session_id FROM claude_sessions WHERE project_name = 'alpha'"
        ).fetchone()
        assert raw["session_id"] is None

    def test_domain_layer_still_sees_empty_string_for_null(
        self, repo: SqliteClaudeSessionRepository
    ) -> None:
        repo.upsert(_sample(session_id=""))
        got = repo.get("alpha")
        assert got is not None
        assert got.session_id == ""

    def test_two_identical_real_session_ids_still_rejected(
        self, conn: sqlite3.Connection, repo: SqliteClaudeSessionRepository
    ) -> None:
        self._seed_extra_project(conn, "beta")
        repo.upsert(_sample(project="alpha", session_id="sess-real"))
        with pytest.raises(sqlite3.IntegrityError):
            repo.upsert(_sample(project="beta", session_id="sess-real"))

    def test_partial_index_exists_in_schema(
        self, conn: sqlite3.Connection
    ) -> None:
        rows = conn.execute(
            "SELECT name, sql FROM sqlite_master "
            "WHERE type='index' AND tbl_name='claude_sessions'"
        ).fetchall()
        names = {r["name"] for r in rows}
        assert "idx_claude_sessions_session_id" in names
        idx_sql = next(
            r["sql"]
            for r in rows
            if r["name"] == "idx_claude_sessions_session_id"
        )
        # Partial — uniqueness only on non-NULL values.
        assert "WHERE" in idx_sql.upper()
        assert "NOT NULL" in idx_sql.upper()
