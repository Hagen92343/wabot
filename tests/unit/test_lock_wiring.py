"""Phase-5 C5.2 wiring smoke.

Verifies that ``SessionService.send_prompt`` honours the injected
``LockService``, that the ``CommandHandler`` surfaces a denial as
the Spec §7 ``🔒 Terminal aktiv`` hint, and that ``TranscriptIngest``
calls the ``on_local_input`` callback for human user-turns.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.application.lock_service import (
    LocalTerminalHoldsLockError,
    LockService,
)
from whatsbot.application.session_service import SessionService
from whatsbot.application.transcript_ingest import TranscriptIngest
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 22, 12, 0, tzinfo=UTC)


@dataclass
class _FakeTmux:
    _alive: set[str] = field(default_factory=set)
    send_text_calls: list[tuple[str, str]] = field(default_factory=list)

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        self.send_text_calls.append((name, text))

    def kill_session(self, name: str) -> bool:
        self._alive.discard(name)
        return True

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        del name, color, label


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    SqliteProjectRepository(c).create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=NOW,
            mode=Mode.NORMAL,
        )
    )
    SqliteClaudeSessionRepository(c).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-alpha",
            transcript_path="",
            started_at=NOW,
            current_mode=Mode.NORMAL,
        )
    )
    try:
        yield c
    finally:
        c.close()


def _build_session_service(
    conn: sqlite3.Connection,
    tmux: _FakeTmux,
    locks: LockService,
    projects_root: Path,
) -> SessionService:
    return SessionService(
        project_repo=SqliteProjectRepository(conn),
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=tmux,
        projects_root=projects_root,
        lock_service=locks,
    )


# ---- SessionService.send_prompt honours the lock -----------------------


def test_send_prompt_raises_when_local_holds_lock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Pre-seed a live local lock just seconds ago.
    SqliteSessionLockRepository(conn).upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW - timedelta(seconds=5),
            last_activity_at=NOW - timedelta(seconds=5),
        )
    )
    locks = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
    )
    tmux = _FakeTmux()
    svc = _build_session_service(conn, tmux, locks, tmp_path)

    with pytest.raises(LocalTerminalHoldsLockError):
        svc.send_prompt("alpha", "hi")
    # The tmux pane never saw the prompt.
    prompts = [text for _, text in tmux.send_text_calls if "​" in text]
    assert prompts == []


def test_send_prompt_grants_after_stale_local_lock(
    conn: sqlite3.Connection, tmp_path: Path
) -> None:
    # Local lock is idle past the timeout → auto-release, bot grant.
    SqliteSessionLockRepository(conn).upsert(
        SessionLock(
            project_name="alpha",
            owner=LockOwner.LOCAL,
            acquired_at=NOW - timedelta(minutes=5),
            last_activity_at=NOW - timedelta(minutes=5),
        )
    )
    locks = LockService(
        repo=SqliteSessionLockRepository(conn),
        clock=lambda: NOW,
    )
    tmux = _FakeTmux()
    svc = _build_session_service(conn, tmux, locks, tmp_path)

    svc.send_prompt("alpha", "hi")  # must not raise
    # ZWSP-prefixed prompt reached the pane.
    zwsp_prompts = [text for _, text in tmux.send_text_calls if "​hi" in text]
    assert len(zwsp_prompts) == 1


# ---- TranscriptIngest fires on_local_input ----------------------------


def test_ingest_fires_on_local_input_for_human_turns() -> None:
    calls: list[str] = []

    @dataclass
    class _StubRepo:
        def upsert(self, session: object) -> None:  # pragma: no cover
            pass

        def get(self, project: str) -> object:  # pragma: no cover
            return None

        def list_all(self) -> list[object]:  # pragma: no cover
            return []

        def delete(self, project: str) -> bool:  # pragma: no cover
            return False

        def update_activity(
            self, project: str, *, tokens_used: int, last_activity_at: datetime
        ) -> None:
            pass

        def bump_turn(self, project: str, *, at: datetime) -> None:  # pragma: no cover
            pass

        def update_mode(self, project: str, mode: Mode) -> None:  # pragma: no cover
            pass

        def mark_compact(self, project: str, at: datetime) -> None:  # pragma: no cover
            pass

    ingest = TranscriptIngest(
        session_repo=_StubRepo(),  # type: ignore[arg-type]
        on_turn_end=lambda _p, _t: None,
        on_local_input=calls.append,
    )

    # Human user-turn — no ZWSP prefix, non-empty text.
    human_line = json.dumps(
        {
            "type": "user",
            "uuid": "u1",
            "timestamp": "t",
            "message": {"content": "what's up"},
        }
    )
    ingest.feed("alpha", human_line)
    assert calls == ["alpha"]

    # Bot-prefixed turn (ZWSP) — MUST NOT fire on_local_input.
    bot_line = json.dumps(
        {
            "type": "user",
            "uuid": "u2",
            "timestamp": "t",
            "message": {"content": "​bot turn"},
        }
    )
    ingest.feed("alpha", bot_line)
    assert calls == ["alpha"]  # unchanged

    # Tool-result user-turn (empty flattened text) — also NO fire.
    tool_result_line = json.dumps(
        {
            "type": "user",
            "uuid": "u3",
            "timestamp": "t",
            "message": {
                "content": [
                    {"type": "tool_result", "tool_use_id": "x", "content": "ok"},
                ]
            },
        }
    )
    ingest.feed("alpha", tool_result_line)
    assert calls == ["alpha"]  # still unchanged
