"""End-to-end smoke for Phase 6 C6.1 ``/stop`` and ``/kill`` via /webhook.

Real ``SubprocessTmuxController`` and a ``safe-claude=/bin/true`` so
the tmux pane has *something* alive. Verifies that:

* ``/stop <name>`` lands a Ctrl+C in the pane (we can't directly observe
  SIGINT, but we can prove ``send-keys C-c`` was the call shape via
  the bot's reply, and that the session stays alive).
* ``/kill <name>`` destroys the tmux session and clears the lock row
  (which we pre-seed to BOT so the release path is exercised).

Skipped when ``tmux`` isn't installed (same gating as the other
/webhook-driven integration tests).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import sqlite3
import subprocess
import time
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.domain.locks import LockOwner, SessionLock
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    KEY_PANIC_PIN,
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
PANIC_PIN = "1234"


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
    base[KEY_PANIC_PIN] = PANIC_PIN
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
                                "phone_number_id": "PID",
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


def _wait_for_tmux_session(name: str, *, timeout: float = 3.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = subprocess.run(
            ["tmux", "has-session", "-t", name],
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return True
        time.sleep(0.05)
    return False


def test_stop_sends_ctrl_c_keeps_session_alive(
    tmp_path: Path, tmux_session_cleanup: list[str]
) -> None:
    project_name = f"k{uuid.uuid4().hex[:6]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = RecordingSender()
    app = create_app(
        settings=Settings(env=Environment.PROD),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
    )

    with TestClient(app) as client:
        # New project + bring the tmux session up.
        r = _signed_post(
            client, _build_meta_payload(f"/new {project_name}")
        )
        assert r.status_code == 200
        r = _signed_post(client, _build_meta_payload(f"/p {project_name}"))
        assert r.status_code == 200
        assert _wait_for_tmux_session(f"wb-{project_name}"), (
            "tmux session should be alive after /p"
        )

        # /stop — Ctrl+C lands; session must stay alive (soft cancel).
        r = _signed_post(
            client, _build_meta_payload(f"/stop {project_name}")
        )
        assert r.status_code == 200

    bodies = [body for _, body in sender.sent]
    assert any("🛑" in body and project_name in body for body in bodies), (
        f"expected /stop ack with stop-emoji, got: {bodies}"
    )

    # tmux session is still alive (Ctrl+C is soft).
    alive = subprocess.run(
        ["tmux", "has-session", "-t", f"wb-{project_name}"],
        capture_output=True,
        check=False,
    )
    assert alive.returncode == 0, "session should survive a /stop"


def test_kill_destroys_session_and_releases_lock(
    tmp_path: Path, tmux_session_cleanup: list[str]
) -> None:
    project_name = f"k{uuid.uuid4().hex[:6]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = RecordingSender()
    app = create_app(
        settings=Settings(env=Environment.PROD),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
    )

    with TestClient(app) as client:
        _signed_post(client, _build_meta_payload(f"/new {project_name}"))
        _signed_post(client, _build_meta_payload(f"/p {project_name}"))
        assert _wait_for_tmux_session(f"wb-{project_name}")

        # Pre-seed a BOT lock so /kill proves the release path.
        lock_conn = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        lock_conn.row_factory = sqlite3.Row
        now = datetime.now(UTC)
        SqliteSessionLockRepository(lock_conn).upsert(
            SessionLock(
                project_name=project_name,
                owner=LockOwner.BOT,
                acquired_at=now,
                last_activity_at=now,
            )
        )
        lock_conn.close()

        r = _signed_post(
            client, _build_meta_payload(f"/kill {project_name}")
        )
        assert r.status_code == 200

    bodies = [body for _, body in sender.sent]
    assert any("🪓" in body and project_name in body for body in bodies)
    assert any("Lock freigegeben" in body for body in bodies)

    # tmux session is gone.
    gone = subprocess.run(
        ["tmux", "has-session", "-t", f"wb-{project_name}"],
        capture_output=True,
        check=False,
    )
    assert gone.returncode != 0, "session should be dead after /kill"

    # Lock row is gone.
    verify_conn = sqlite_repo.connect(str(db_path))
    row = verify_conn.execute(
        "SELECT owner FROM session_locks WHERE project_name = ?",
        (project_name,),
    ).fetchone()
    verify_conn.close()
    assert row is None
