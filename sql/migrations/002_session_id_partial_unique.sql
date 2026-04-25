-- Migration 002 — Mini-Phase 12 (post Phase 11)
-- Replaces the column-level ``UNIQUE`` constraint on
-- ``claude_sessions.session_id`` with a partial unique index that only
-- enforces uniqueness on non-NULL values.
--
-- Why: the placeholder used by SessionService before Claude has produced
-- a real session_id is the empty string. Two such rows (e.g. ``scratch``
-- + a freshly-imported project) collide on the column-level UNIQUE,
-- which crashes ``ensure_started`` with ``UNIQUE constraint failed:
-- claude_sessions.session_id``. The partial index keeps the safety net
-- against duplicate *real* IDs while letting NULL placeholders coexist.
--
-- Migration also normalises any existing empty-string session_id to NULL
-- via ``NULLIF`` so the partial index has consistent semantics on disk.
--
-- SQLite cannot drop a column-level constraint via ``ALTER TABLE``, so we
-- use the rename-copy-drop-rename pattern (same as 001). FK references to
-- ``claude_sessions`` are name-based via ``project_name`` and remain valid.
--
-- The migration runner toggles ``PRAGMA foreign_keys = OFF`` around this
-- script and runs ``foreign_key_check`` afterwards. Transaction control
-- lives inside the script (executescript() forbids an outer txn).

BEGIN TRANSACTION;

CREATE TABLE claude_sessions_new (
    project_name TEXT PRIMARY KEY REFERENCES projects(name) ON DELETE CASCADE,
    session_id TEXT,
    transcript_path TEXT,
    started_at TEXT NOT NULL,
    turns_count INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    context_fill_ratio REAL DEFAULT 0.0,
    last_compact_at TEXT,
    last_activity_at TEXT,
    current_mode TEXT DEFAULT 'normal' CHECK(current_mode IN ('normal', 'strict', 'yolo'))
);

INSERT INTO claude_sessions_new
    (project_name, session_id, transcript_path, started_at, turns_count,
     tokens_used, context_fill_ratio, last_compact_at, last_activity_at,
     current_mode)
SELECT project_name, NULLIF(session_id, ''), transcript_path, started_at,
       turns_count, tokens_used, context_fill_ratio, last_compact_at,
       last_activity_at, current_mode
FROM claude_sessions;

DROP TABLE claude_sessions;
ALTER TABLE claude_sessions_new RENAME TO claude_sessions;

CREATE UNIQUE INDEX idx_claude_sessions_session_id
    ON claude_sessions(session_id)
    WHERE session_id IS NOT NULL;

PRAGMA user_version = 2;

COMMIT;
