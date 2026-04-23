"""Unit tests for DiagnosticsService — Phase 8 C8.2."""

from __future__ import annotations

from datetime import UTC, datetime

from whatsbot.application.diagnostics_service import (
    DEFAULT_ERRORS_LIMIT,
    MAX_TRACE_EVENTS,
    DiagnosticsService,
    SessionSnapshot,
)
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.log_events import LogEntry
from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import ClaudeSession


class FakeLogReader:
    def __init__(self, entries: list[LogEntry]) -> None:
        self._entries = entries
        self.last_max_lines: int | None = None

    def read_tail(self, *, max_lines: int) -> list[LogEntry]:
        self.last_max_lines = max_lines
        return list(self._entries)


class FakeClaudeSessionRepository:
    def __init__(self, rows: list[ClaudeSession]) -> None:
        self._rows = rows

    def upsert(self, session: ClaudeSession) -> None:  # pragma: no cover
        raise NotImplementedError

    def get(self, project_name: str) -> ClaudeSession | None:  # pragma: no cover
        for row in self._rows:
            if row.project_name == project_name:
                return row
        return None

    def list_all(self) -> list[ClaudeSession]:
        return list(self._rows)

    def delete(self, project_name: str) -> bool:  # pragma: no cover
        return False

    def update_activity(self, project_name: str, *, at: datetime) -> None:
        return None  # pragma: no cover

    def bump_turn(self, project_name: str, *, at: datetime) -> None:
        return None  # pragma: no cover

    def update_mode(self, project_name: str, mode: Mode) -> None:
        return None  # pragma: no cover

    def mark_compact(self, project_name: str, at: datetime) -> None:
        return None  # pragma: no cover


class FakeLockRepo:
    def __init__(self, locks: dict[str, LockOwner]) -> None:
        self._locks = locks

    def get(self, project_name: str) -> SessionLock | None:
        owner = self._locks.get(project_name)
        if owner is None:
            return None
        return SessionLock(
            project_name=project_name,
            owner=owner,
            acquired_at=0,
            last_activity_at=0,
        )

    def upsert(self, lock: SessionLock) -> None:
        return None  # pragma: no cover

    def delete(self, project_name: str) -> bool:
        return False  # pragma: no cover

    def list_all(self) -> list[SessionLock]:
        return []  # pragma: no cover


class FakeTmux:
    def __init__(self, alive: list[str]) -> None:
        self._alive = alive
        self.list_calls = 0

    def has_session(self, name: str) -> bool:  # pragma: no cover
        return name in self._alive

    def new_session(self, name: str, *, cwd) -> None:  # pragma: no cover
        return None

    def send_text(self, name: str, text: str) -> None:  # pragma: no cover
        return None

    def kill_session(self, name: str) -> bool:  # pragma: no cover
        return True

    def interrupt(self, name: str) -> None:  # pragma: no cover
        return None

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        self.list_calls += 1
        if prefix is None:
            return list(self._alive)
        return [n for n in self._alive if n.startswith(prefix)]

    def set_status(self, name: str, *, color: str, label: str) -> None:
        return None  # pragma: no cover


# ---- read_trace -----------------------------------------------------


def test_read_trace_filters_on_msg_id_and_preserves_order() -> None:
    entries = [
        LogEntry(event="webhook_in", msg_id="m1", ts="1"),
        LogEntry(event="route", msg_id="m2", ts="2"),
        LogEntry(event="command_routed", msg_id="m1", ts="3"),
        LogEntry(event="deliver", msg_id="m1", ts="4"),
    ]
    svc = DiagnosticsService(log_reader=FakeLogReader(entries))

    trace = svc.read_trace("m1")

    assert [e.event for e in trace] == ["webhook_in", "command_routed", "deliver"]


def test_read_trace_caps_at_max_trace_events() -> None:
    entries = [
        LogEntry(event=f"e{i}", msg_id="same", ts=str(i))
        for i in range(MAX_TRACE_EVENTS + 50)
    ]
    svc = DiagnosticsService(log_reader=FakeLogReader(entries))

    trace = svc.read_trace("same")

    assert len(trace) == MAX_TRACE_EVENTS
    # Most-recent events survive (we keep the tail).
    assert trace[-1].event == f"e{MAX_TRACE_EVENTS + 49}"


def test_read_trace_empty_for_unknown_msg_id() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))

    assert svc.read_trace("never-seen") == []


def test_read_trace_uses_tail_limit_on_reader() -> None:
    reader = FakeLogReader([])
    svc = DiagnosticsService(log_reader=reader, tail_limit=123)

    svc.read_trace("x")

    assert reader.last_max_lines == 123


def test_format_trace_single_source_of_truth() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    entries = [
        LogEntry(
            event="command_routed",
            ts="2026-04-21T00:00:00Z",
            level="INFO",
            msg_id="m1",
            project="alpha",
        )
    ]

    rendered = svc.format_trace("m1", entries)

    assert "Trace msg_id=m1" in rendered
    assert "command_routed" in rendered
    assert "project=alpha" in rendered


def test_format_trace_no_entries_hint() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    assert "kein Trace" in svc.format_trace("m1", [])


# ---- recent_errors ------------------------------------------------


def test_recent_errors_filters_and_caps_at_limit() -> None:
    entries = [
        LogEntry(event=f"warn{i}", level="WARNING", ts=str(i))
        for i in range(DEFAULT_ERRORS_LIMIT + 5)
    ]
    entries.insert(3, LogEntry(event="ok", level="INFO"))
    svc = DiagnosticsService(log_reader=FakeLogReader(entries))

    errors = svc.recent_errors()

    assert len(errors) == DEFAULT_ERRORS_LIMIT
    assert all(e.is_error for e in errors)


def test_recent_errors_limit_zero_returns_empty() -> None:
    svc = DiagnosticsService(
        log_reader=FakeLogReader([LogEntry(level="ERROR", event="boom")])
    )
    assert svc.recent_errors(limit=0) == []
    assert svc.recent_errors(limit=-1) == []


def test_format_errors_empty_is_friendly() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    rendered = svc.format_errors([])
    assert "keine Fehler" in rendered


def test_format_errors_includes_msg_id_for_triage() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    entries = [LogEntry(event="boom", level="ERROR", msg_id="m42", ts="t")]
    rendered = svc.format_errors(entries)
    assert "boom" in rendered
    assert "msg_id=m42" in rendered
    assert "ERROR" in rendered


# ---- active_sessions ----------------------------------------------


def _session(name: str, *, mode: Mode = Mode.NORMAL, tokens: int = 0) -> ClaudeSession:
    return ClaudeSession(
        project_name=name,
        session_id=f"sid-{name}",
        transcript_path=f"/tmp/{name}.jsonl",
        started_at=datetime.now(UTC),
        current_mode=mode,
        turns_count=3,
        tokens_used=tokens,
        context_fill_ratio=tokens / 200_000 if tokens else 0.0,
    )


def test_active_sessions_joins_claude_db_tmux_and_lock() -> None:
    rows = [_session("alpha", mode=Mode.NORMAL, tokens=40000)]
    svc = DiagnosticsService(
        log_reader=FakeLogReader([]),
        claude_sessions=FakeClaudeSessionRepository(rows),
        locks=FakeLockRepo({"alpha": LockOwner.BOT}),
        tmux=FakeTmux(alive=["wb-alpha"]),
    )

    snaps = svc.active_sessions()

    assert len(snaps) == 1
    snap = snaps[0]
    assert isinstance(snap, SessionSnapshot)
    assert snap.project_name == "alpha"
    assert snap.mode == Mode.NORMAL
    assert snap.tmux_alive is True
    assert snap.lock_owner == LockOwner.BOT
    assert snap.turns_count == 3
    assert snap.tokens_used == 40000


def test_active_sessions_marks_db_orphan_as_dead() -> None:
    rows = [_session("alpha"), _session("beta")]
    svc = DiagnosticsService(
        log_reader=FakeLogReader([]),
        claude_sessions=FakeClaudeSessionRepository(rows),
        locks=FakeLockRepo({}),
        tmux=FakeTmux(alive=["wb-alpha"]),
    )

    snaps = svc.active_sessions()

    by_name = {s.project_name: s for s in snaps}
    assert by_name["alpha"].tmux_alive is True
    assert by_name["beta"].tmux_alive is False
    # No lock row → FREE by default.
    assert by_name["alpha"].lock_owner == LockOwner.FREE


def test_active_sessions_empty_without_repos() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    assert svc.active_sessions() == []


def test_active_sessions_empty_db_returns_empty() -> None:
    svc = DiagnosticsService(
        log_reader=FakeLogReader([]),
        claude_sessions=FakeClaudeSessionRepository([]),
        locks=FakeLockRepo({}),
        tmux=FakeTmux(alive=[]),
    )
    assert svc.active_sessions() == []


def test_format_sessions_renders_every_snapshot() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    snaps = [
        SessionSnapshot(
            project_name="alpha",
            mode=Mode.NORMAL,
            tmux_alive=True,
            turns_count=5,
            tokens_used=160000,
            context_fill_ratio=0.80,
            lock_owner=LockOwner.BOT,
        ),
        SessionSnapshot(
            project_name="beta",
            mode=Mode.STRICT,
            tmux_alive=False,
            turns_count=0,
            tokens_used=0,
            context_fill_ratio=0.0,
            lock_owner=LockOwner.FREE,
        ),
    ]

    rendered = svc.format_sessions(snaps)

    assert "alpha" in rendered
    assert "beta" in rendered
    assert "80%" in rendered
    # Alive dot vs dead dot show up both.
    assert "🟢" in rendered
    assert "⚫" in rendered


def test_format_sessions_empty_is_friendly() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    assert "keine aktiven" in svc.format_sessions([])


# ---- /update -------------------------------------------------------


def test_format_update_hint_mentions_manual_procedure() -> None:
    svc = DiagnosticsService(log_reader=FakeLogReader([]))
    hint = svc.format_update_hint()
    assert "manuell" in hint
    assert "RUNBOOK" in hint
