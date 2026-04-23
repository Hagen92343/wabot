"""End-to-end smoke for ``/mode strict`` via ``/webhook``.

Phase-4 C4.3: switching mode recycles the tmux session with the
new permission-mode flag, persists both ``projects.mode`` and
``claude_sessions.current_mode``, and writes a ``mode_events``
switch row. The status bar is repainted to the new mode's colour.

Skipped when ``tmux`` isn't installed.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import sqlite3
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.domain.projects import Mode
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


APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ALLOWED_SENDER = "+491701234567"


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
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return StubSecrets(**base)


def _build_meta_payload(text: str) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "TEST_WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "+491700000000",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [{"wa_id": "491701234567"}],
                            "messages": [
                                {
                                    "from": ALLOWED_SENDER,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": "1745318400",
                                    "text": {"body": text},
                                    "type": "text",
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def _signed_post(client: TestClient, body: bytes) -> httpx.Response:
    sig = "sha256=" + hmac.new(
        APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )


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


def test_mode_strict_recycles_session_and_writes_audit(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"mode{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

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
        # /bin/true exits instantly — the launch path doesn't need
        # a real Claude for this test; we only check tmux lifecycle
        # + DB side-effects.
        safe_claude_binary="/bin/true",
    )

    with TestClient(app) as client:
        r = _signed_post(
            client, _build_meta_payload(f"/new {project_name}")
        )
        assert r.status_code == 200
        r = _signed_post(
            client, _build_meta_payload(f"/p {project_name}")
        )
        assert r.status_code == 200
        # Project is in Normal after /p.
        assert _fetch_mode(conn, project_name) is Mode.NORMAL

        # Switch to Strict.
        r = _signed_post(client, _build_meta_payload("/mode strict"))
        assert r.status_code == 200

    # projects.mode + claude_sessions.current_mode both flipped.
    assert _fetch_mode(conn, project_name) is Mode.STRICT
    session_row = _fetch_session_mode(conn, project_name)
    assert session_row is Mode.STRICT

    # mode_events row recorded.
    events = SqliteModeEventRepository(conn).list_for_project(project_name)
    assert len(events) == 1
    ev = events[0]
    assert ev.from_mode is Mode.NORMAL
    assert ev.to_mode is Mode.STRICT
    assert ev.kind.value == "switch"

    # tmux session still exists (it was killed + re-created).
    ctrl = SubprocessTmuxController()
    assert ctrl.has_session(f"wb-{project_name}") is True


def test_mode_yolo_then_strict_writes_two_audit_rows(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"mode{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

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
    )
    with TestClient(app) as client:
        _signed_post(client, _build_meta_payload(f"/new {project_name}"))
        _signed_post(client, _build_meta_payload(f"/p {project_name}"))
        _signed_post(client, _build_meta_payload("/mode yolo"))
        _signed_post(client, _build_meta_payload("/mode strict"))

    events = SqliteModeEventRepository(conn).list_for_project(project_name)
    # Newest first per list_for_project's ORDER BY.
    assert len(events) == 2
    assert events[0].from_mode is Mode.YOLO
    assert events[0].to_mode is Mode.STRICT
    assert events[1].from_mode is Mode.NORMAL
    assert events[1].to_mode is Mode.YOLO
    assert _fetch_mode(conn, project_name) is Mode.STRICT


def _fetch_mode(conn: sqlite3.Connection, name: str) -> Mode:
    row = conn.execute(
        "SELECT mode FROM projects WHERE name = ?", (name,)
    ).fetchone()
    assert row is not None
    return Mode(row["mode"])


def _fetch_session_mode(
    conn: sqlite3.Connection, name: str
) -> Mode | None:
    row = conn.execute(
        "SELECT current_mode FROM claude_sessions WHERE project_name = ?",
        (name,),
    ).fetchone()
    if row is None:
        return None
    return Mode(row["current_mode"])
