-- whatsbot state schema.
-- Spec §19. Single source of truth for the SQLite state-DB.
--
-- PRAGMAs are NOT in this file because they are per-connection and live in
-- whatsbot/adapters/sqlite_repo.py (PRAGMAS tuple). This file is purely DDL
-- so it can also be eyeballed via `sqlite3 ... ".schema"`.

CREATE TABLE projects (
    name TEXT PRIMARY KEY,
    source_mode TEXT NOT NULL CHECK(source_mode IN ('empty', 'git')),
    source TEXT,
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    default_model TEXT DEFAULT 'sonnet',
    mode TEXT DEFAULT 'normal' CHECK(mode IN ('normal', 'strict', 'yolo'))
);

CREATE TABLE claude_sessions (
    project_name TEXT PRIMARY KEY REFERENCES projects(name) ON DELETE CASCADE,
    session_id TEXT UNIQUE,
    transcript_path TEXT,
    started_at TEXT NOT NULL,
    turns_count INTEGER DEFAULT 0,
    tokens_used INTEGER DEFAULT 0,
    context_fill_ratio REAL DEFAULT 0.0,
    last_compact_at TEXT,
    last_activity_at TEXT,
    current_mode TEXT DEFAULT 'normal' CHECK(current_mode IN ('normal', 'strict', 'yolo'))
);

CREATE TABLE session_locks (
    project_name TEXT PRIMARY KEY REFERENCES projects(name) ON DELETE CASCADE,
    owner TEXT NOT NULL CHECK(owner IN ('bot', 'local', 'free')),
    acquired_at INTEGER NOT NULL,
    last_activity_at INTEGER NOT NULL,
    integrity_hash TEXT
);

CREATE TABLE pending_deletes (
    project_name TEXT PRIMARY KEY,
    deadline_ts INTEGER NOT NULL
);

CREATE TABLE pending_confirmations (
    id TEXT PRIMARY KEY,
    project_name TEXT,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    deadline_ts INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    msg_id TEXT
);

CREATE TABLE max_limits (
    kind TEXT PRIMARY KEY CHECK(kind IN ('session_5h', 'weekly', 'opus_sub')),
    reset_at_ts INTEGER NOT NULL,
    warned_at_ts INTEGER,
    remaining_pct REAL
);

CREATE TABLE pending_outputs (
    msg_id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    output_path TEXT NOT NULL,
    size_bytes INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    deadline_ts INTEGER NOT NULL
);

CREATE TABLE app_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- Reserved app_state rows: 'active_project', 'lockdown', 'version', 'last_heartbeat'.

CREATE TABLE mode_events (
    id TEXT PRIMARY KEY,
    project_name TEXT NOT NULL,
    event TEXT CHECK(event IN ('switch', 'reboot_reset', 'panic_reset', 'session_recycle')),
    from_mode TEXT,
    to_mode TEXT,
    ts INTEGER NOT NULL,
    msg_id TEXT
);

CREATE TABLE allow_rules (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    project_name TEXT NOT NULL,
    tool TEXT NOT NULL,
    pattern TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT CHECK(source IN ('default', 'smart_detection', 'manual')),
    FOREIGN KEY (project_name) REFERENCES projects(name) ON DELETE CASCADE
);

CREATE INDEX idx_locks_activity ON session_locks(last_activity_at);
CREATE INDEX idx_limits_reset ON max_limits(reset_at_ts);
CREATE INDEX idx_pending_deadline ON pending_confirmations(deadline_ts);
CREATE INDEX idx_mode_events_ts ON mode_events(ts);
CREATE INDEX idx_allow_rules_project ON allow_rules(project_name);
