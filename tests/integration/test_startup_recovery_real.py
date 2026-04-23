"""Integration smoke for StartupRecovery against a real create_app.

Phase-4 C4.6 + C4.7: after a bot restart the YOLO coercion fires
before Claude is resumed, and every ``claude_sessions`` row is
relaunched via ``safe-claude --resume <id>``.

We simulate the "process restart" by constructing a fresh app on
top of a pre-seeded on-disk DB. Same DB file + same tmp projects
root, but a brand-new ``SessionService`` + ``WatchdogTranscriptWatcher``
— exactly the shape that a launchd-restart produces.

Skipped when ``tmux`` isn't installed.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.domain.mode_events import ModeEventKind
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    SecretNotFoundError,
)

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        shutil.which("tmux") is None, reason="tmux not installed"
    ),
]


class RecordingSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


class StubSecrets:
    def __init__(self, **kv: str) -> None:
        self._store = dict(kv)

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


def _full_secret_stub() -> StubSecrets:
    base = {key: f"placeholder-for-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = "sig"
    base[KEY_META_VERIFY_TOKEN] = "verify"
    base[KEY_ALLOWED_SENDERS] = "+491701234567"
    return StubSecrets(**base)


@pytest.fixture
def tmux_session_cleanup() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            check=False,
        )


def _preseed_db(
    conn: sqlite3.Connection,
    name: str,
    mode: Mode,
    session_id: str,
) -> None:
    SqliteProjectRepository(conn).create(
        Project(
            name=name,
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=mode,
        )
    )
    SqliteClaudeSessionRepository(conn).upsert(
        ClaudeSession(
            project_name=name,
            session_id=session_id,
            transcript_path="",
            started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            current_mode=mode,
        )
    )


def test_recovery_resets_yolo_and_relaunches_session(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"rec{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project_name}")
    session_id = f"sess-{uuid.uuid4().hex}"

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    (projects_root / project_name).mkdir()
    db_path = tmp_path / "state.db"

    # Pre-seed the DB as if the bot had crashed mid-session in YOLO.
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)
    _preseed_db(conn, project_name, Mode.YOLO, session_id)
    conn.close()

    # Fresh create_app — this is the "bot started up after a crash"
    # moment. Give it the same DB file so it sees the stranded row.
    conn = sqlite_repo.connect(str(db_path))
    app = create_app(
        settings=Settings(env=Environment.PROD),
        secrets_provider=_full_secret_stub(),
        message_sender=RecordingSender(),
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        run_startup_recovery=True,
    )
    del app  # we only care about the side-effects of create_app here.

    # 1. projects.mode was coerced YOLO → Normal (Spec §6).
    assert SqliteProjectRepository(conn).get(project_name).mode is Mode.NORMAL

    # 2. A reboot_reset audit row was written.
    events = SqliteModeEventRepository(conn).list_for_project(project_name)
    kinds = [e.kind for e in events]
    assert ModeEventKind.REBOOT_RESET in kinds
    reset_event = next(e for e in events if e.kind is ModeEventKind.REBOOT_RESET)
    assert reset_event.from_mode is Mode.YOLO
    assert reset_event.to_mode is Mode.NORMAL

    # 3. tmux session for the project is back.
    ctrl = SubprocessTmuxController()
    assert ctrl.has_session(f"wb-{project_name}") is True

    conn.close()


def test_recovery_is_noop_when_no_sessions_preexist(
    tmp_path: Path,
) -> None:
    """A fresh install has no rows — recovery must not crash and
    must not fabricate any mode_events entries."""
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"

    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    app = create_app(
        settings=Settings(env=Environment.PROD),
        secrets_provider=_full_secret_stub(),
        message_sender=RecordingSender(),
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        run_startup_recovery=True,
    )
    recovery = app.state.startup_recovery
    assert recovery is not None
    # No mode_events rows should have been written for any project.
    # (The list_for_project("ghost") contract returns [] for unknown
    # projects, which is a weak check — we assert the full table is
    # empty via a direct SELECT.)
    rows = conn.execute("SELECT COUNT(*) AS n FROM mode_events").fetchone()
    assert rows["n"] == 0

    conn.close()
