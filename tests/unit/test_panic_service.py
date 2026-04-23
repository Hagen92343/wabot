"""Unit tests for ``whatsbot.application.panic_service`` (Phase 6 C6.2 + C6.3)."""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_app_state_repository import SqliteAppStateRepository
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.application.lock_service import LockService
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.application.panic_service import (
    DEFAULT_CLAUDE_PROCESS_PATTERN,
    PanicService,
)
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.mode_events import ModeEventKind
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.process_killer import (
    KillResult,
    ProcessKillerError,
)

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    kill_session_calls: list[str] = field(default_factory=list)
    list_sessions_calls: list[str | None] = field(default_factory=list)

    def has_session(self, name: str) -> bool:  # pragma: no cover
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:  # pragma: no cover
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:  # pragma: no cover
        del name, text

    def interrupt(self, name: str) -> None:  # pragma: no cover
        del name

    def kill_session(self, name: str) -> bool:
        self.kill_session_calls.append(name)
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        self.list_sessions_calls.append(prefix)
        names = sorted(self._alive)
        if prefix is None:
            return names
        return [n for n in names if n.startswith(prefix)]

    def set_status(self, name: str, *, color: str, label: str) -> None:  # pragma: no cover
        del name, color, label


@dataclass
class _FakeProcessKiller:
    matched_per_pattern: dict[str, int] = field(default_factory=dict)
    raise_on_call: Exception | None = None
    calls: list[str] = field(default_factory=list)

    def kill_by_pattern(self, pattern: str) -> KillResult:
        self.calls.append(pattern)
        if self.raise_on_call is not None:
            raise self.raise_on_call
        matched = self.matched_per_pattern.get(pattern, 0)
        return KillResult(
            pattern=pattern,
            exit_code=0 if matched > 0 else 1,
            matched_count=matched,
        )


@dataclass
class _FakeNotifier:
    calls: list[tuple[str, str, bool]] = field(default_factory=list)
    raise_on_call: Exception | None = None

    def send(self, *, title: str, body: str, sound: bool = False) -> None:
        if self.raise_on_call is not None:
            raise self.raise_on_call
        self.calls.append((title, body, sound))


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    project_repo = SqliteProjectRepository(c)
    project_repo.create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.NORMAL,
        )
    )
    project_repo.create(
        Project(
            name="beta",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.YOLO,
        )
    )
    project_repo.create(
        Project(
            name="gamma",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.YOLO,
        )
    )
    try:
        yield c
    finally:
        c.close()


def _build(
    conn: sqlite3.Connection,
    *,
    panic_marker: Path,
    alive_sessions: set[str] | None = None,
    locks: list[SessionLock] | None = None,
    notifier: _FakeNotifier | None = None,
    killer: _FakeProcessKiller | None = None,
    monotonic_seq: list[float] | None = None,
) -> tuple[
    PanicService,
    _FakeTmux,
    LockdownService,
    LockService,
    _FakeProcessKiller,
    _FakeNotifier,
]:
    tmux = _FakeTmux(_alive=set(alive_sessions or set()))
    project_repo = SqliteProjectRepository(conn)
    mode_event_repo = SqliteModeEventRepository(conn)
    lock_repo = SqliteSessionLockRepository(conn)
    if locks:
        for lk in locks:
            lock_repo.upsert(lk)
    lock_service = LockService(repo=lock_repo, clock=lambda: NOW)
    lockdown = LockdownService(
        app_state=SqliteAppStateRepository(conn),
        panic_marker_path=panic_marker,
        clock=lambda: NOW,
    )
    process_killer = killer if killer is not None else _FakeProcessKiller()
    notif = notifier if notifier is not None else _FakeNotifier()
    monotonic_iter = (
        iter([0.0, 0.123]) if monotonic_seq is None else iter(monotonic_seq)
    )
    svc = PanicService(
        tmux=tmux,
        project_repo=project_repo,
        mode_event_repo=mode_event_repo,
        lock_service=lock_service,
        lockdown_service=lockdown,
        process_killer=process_killer,
        notifier=notif,
        clock=lambda: NOW,
        monotonic=lambda: next(monotonic_iter),
    )
    return svc, tmux, lockdown, lock_service, process_killer, notif


# ---- happy path: full panic flow ---------------------------------


def test_panic_runs_full_playbook(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    marker = tmp_path / "PANIC"
    svc, tmux, lockdown, _, killer, notif = _build(
        conn,
        panic_marker=marker,
        alive_sessions={"wb-alpha", "wb-beta", "wb-gamma", "other"},
        locks=[
            SessionLock("alpha", LockOwner.BOT, NOW, NOW),
            SessionLock("beta", LockOwner.LOCAL, NOW, NOW),
            SessionLock("gamma", LockOwner.BOT, NOW, NOW),
        ],
        killer=_FakeProcessKiller(matched_per_pattern={"safe-claude": 3}),
    )

    outcome = svc.panic()

    # 1. Lockdown engaged + marker on disk.
    assert lockdown.is_engaged() is True
    assert marker.exists()

    # 2. Only wb-* killed; "other" untouched.
    assert sorted(outcome.sessions_killed) == [
        "wb-alpha",
        "wb-beta",
        "wb-gamma",
    ]
    assert "other" not in tmux.kill_session_calls

    # 3. pkill called with default pattern.
    assert killer.calls == [DEFAULT_CLAUDE_PROCESS_PATTERN]
    assert outcome.process_killer_result is not None
    assert outcome.process_killer_result.matched_count == 3

    # 4. Both YOLO projects flipped to Normal + audit row.
    project_repo = SqliteProjectRepository(conn)
    assert project_repo.get("beta").mode is Mode.NORMAL
    assert project_repo.get("gamma").mode is Mode.NORMAL
    assert project_repo.get("alpha").mode is Mode.NORMAL  # unchanged
    assert sorted(outcome.yolo_projects_reset) == ["beta", "gamma"]

    # 5. mode_events rows for the YOLO resets.
    audit_rows = conn.execute(
        "SELECT project_name, event, from_mode, to_mode FROM mode_events "
        "WHERE event = ? ORDER BY project_name",
        (ModeEventKind.PANIC_RESET.value,),
    ).fetchall()
    assert [r["project_name"] for r in audit_rows] == ["beta", "gamma"]
    for row in audit_rows:
        assert row["from_mode"] == Mode.YOLO.value
        assert row["to_mode"] == Mode.NORMAL.value

    # 6. Locks cleared for all 3 projects.
    lock_repo = SqliteSessionLockRepository(conn)
    assert lock_repo.get("alpha") is None
    assert lock_repo.get("beta") is None
    assert lock_repo.get("gamma") is None
    assert sorted(outcome.locks_released) == ["alpha", "beta", "gamma"]

    # 7. Notification fired with sound.
    assert len(notif.calls) == 1
    title, body, sound = notif.calls[0]
    assert "PANIC" in title
    assert "3 Sessions" in body
    assert "2 YOLO" in body
    assert sound is True

    # Latency budget — fake monotonic returned 0.123s.
    assert outcome.duration_seconds == pytest.approx(0.123)


# ---- ordering: lockdown happens FIRST -----------------------------


def test_lockdown_engages_before_session_kills(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Spec §7 invariant: lockdown must land before we tear down
    sessions, otherwise a concurrent webhook could restart something
    we just killed."""
    events: list[str] = []

    svc, tmux, lockdown, _, _, _ = _build(
        conn,
        panic_marker=tmp_path / "PANIC",
        alive_sessions={"wb-alpha"},
    )

    # Monkey-patch the recorded operations to log their order.
    original_engage = lockdown.engage
    original_kill = tmux.kill_session

    def recording_engage(*args: object, **kwargs: object):  # type: ignore[no-untyped-def]
        events.append("engage")
        return original_engage(*args, **kwargs)  # type: ignore[arg-type]

    def recording_kill(name: str) -> bool:
        events.append(f"kill:{name}")
        return original_kill(name)

    lockdown.engage = recording_engage  # type: ignore[method-assign]
    tmux.kill_session = recording_kill  # type: ignore[method-assign]

    svc.panic()
    assert events[0] == "engage"
    assert events[1].startswith("kill:")


# ---- defensive: missing optionals + failures ---------------------


def test_panic_runs_without_notifier(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    svc, _, lockdown, _, _, _ = _build(
        conn, panic_marker=tmp_path / "PANIC", notifier=_FakeNotifier()
    )
    # Replace the notifier with None directly: simulating the
    # "no macOS dep available" startup path.
    svc._notifier = None  # type: ignore[attr-defined]
    outcome = svc.panic()
    assert lockdown.is_engaged() is True
    assert outcome.duration_seconds >= 0


def test_panic_swallows_process_killer_failure(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """A broken pkill shouldn't abort the rest of the playbook."""
    killer = _FakeProcessKiller(
        raise_on_call=ProcessKillerError("pkill missing")
    )
    svc, _, lockdown, _, _, _ = _build(
        conn,
        panic_marker=tmp_path / "PANIC",
        alive_sessions={"wb-alpha"},
        killer=killer,
    )
    outcome = svc.panic()
    assert outcome.process_killer_result is None
    assert outcome.sessions_killed == ("wb-alpha",)
    assert lockdown.is_engaged() is True


def test_panic_swallows_notifier_failure(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    notif = _FakeNotifier(raise_on_call=RuntimeError("ouch"))
    svc, _, lockdown, _, _, _ = _build(
        conn,
        panic_marker=tmp_path / "PANIC",
        alive_sessions={"wb-alpha"},
        notifier=notif,
    )
    outcome = svc.panic()  # must not raise
    assert outcome.sessions_killed == ("wb-alpha",)
    assert lockdown.is_engaged() is True


def test_panic_does_not_kill_non_wb_sessions(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """The user's own tmux sessions (anything not ``wb-*``) must
    survive a /panic."""
    svc, tmux, _, _, _, _ = _build(
        conn,
        panic_marker=tmp_path / "PANIC",
        alive_sessions={"wb-alpha", "personal", "scratch"},
    )
    svc.panic()
    assert "personal" not in tmux.kill_session_calls
    assert "scratch" not in tmux.kill_session_calls


# ---- C6.3 — YOLO reset audit invariant ---------------------------


def test_panic_writes_panic_reset_audit_rows_only_for_yolo(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """alpha is Normal — no audit row should land for it."""
    svc, _, _, _, _, _ = _build(
        conn, panic_marker=tmp_path / "PANIC"
    )
    svc.panic()
    rows = conn.execute(
        "SELECT project_name FROM mode_events WHERE event = ?",
        (ModeEventKind.PANIC_RESET.value,),
    ).fetchall()
    names = sorted(r["project_name"] for r in rows)
    assert names == ["beta", "gamma"]
    # alpha was Normal — no row.
    assert "alpha" not in names


def test_panic_idempotent_second_run_still_safe(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Running panic twice in a row mustn't crash on the empty-YOLO
    second run, and lockdown stays engaged with the *first* timestamp."""
    svc, _, lockdown, _, _, _ = _build(
        conn,
        panic_marker=tmp_path / "PANIC",
        # Two full panic runs → 4 monotonic reads.
        monotonic_seq=[0.0, 0.05, 0.1, 0.15],
    )
    first = svc.panic()
    second = svc.panic()
    assert second.sessions_killed == ()
    assert second.yolo_projects_reset == ()
    # Lockdown is still engaged but its engaged_at didn't move.
    assert lockdown.current().engaged_at == first.lockdown_state.engaged_at


# ---- latency budget proxy (FakeTmux is fast) ---------------------


def test_panic_under_2s_with_fake_tmux(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    """Spec §21 Phase 6 C6.2: panic must land in < 2 s. With fakes
    the budget is trivially met — but the assertion documents the
    contract for anyone tweaking the playbook later."""
    svc, _, _, _, _, _ = _build(
        conn,
        panic_marker=tmp_path / "PANIC",
        alive_sessions={"wb-alpha", "wb-beta", "wb-gamma"},
        monotonic_seq=[0.0, 1.5],  # fake total of 1.5 s
    )
    outcome = svc.panic()
    assert outcome.duration_seconds < 2.0
