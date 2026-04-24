-- Migration 001 — Phase 11 /import
-- Adds: projects.path TEXT (nullable). NULL = implicit ~/projekte/<name>.
-- Extends: projects.source_mode CHECK to include 'imported'.
--
-- SQLite can't ALTER a CHECK constraint directly, so we rebuild the
-- projects table via the rename-copy-drop-rename pattern. Foreign-key
-- references to projects.name from claude_sessions, session_locks,
-- mode_events, allow_rules stay name-based and remain valid.
--
-- NOTE: The migration runner switches ``PRAGMA foreign_keys = OFF``
-- before running this script and back ON afterwards (PRAGMA can't be
-- toggled inside a transaction in autocommit mode). Transaction control
-- lives *inside* this script because Python's ``executescript()`` forbids
-- an outer transaction.

BEGIN TRANSACTION;

CREATE TABLE projects_new (
    name TEXT PRIMARY KEY,
    source_mode TEXT NOT NULL CHECK(source_mode IN ('empty', 'git', 'imported')),
    source TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    default_model TEXT DEFAULT 'sonnet',
    mode TEXT DEFAULT 'normal' CHECK(mode IN ('normal', 'strict', 'yolo')),
    path TEXT
);

INSERT INTO projects_new
    (name, source_mode, source, created_at, last_used_at, default_model, mode, path)
SELECT name, source_mode, source, created_at, last_used_at, default_model, mode, NULL
FROM projects;

DROP TABLE projects;
ALTER TABLE projects_new RENAME TO projects;

-- Bump user_version inside the same transaction so a mid-migration crash
-- leaves v0 intact (nothing half-done "sticks" with the new version bump).
PRAGMA user_version = 1;

COMMIT;
