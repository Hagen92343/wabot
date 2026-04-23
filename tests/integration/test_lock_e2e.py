"""End-to-end smoke for Spec §7 soft-preemption via ``/webhook``.

Shows that a pre-existing ``local`` lock on a project causes an
incoming ``/p <name> <prompt>`` to come back with the
``🔒 Terminal aktiv`` hint — without the prompt ever reaching
tmux. Also verifies ``/release`` clears the lock.

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
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
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


def test_prompt_rejected_while_local_lock_active(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"lock{uuid.uuid4().hex[:4]}"
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
        r = _signed_post(
            client, _build_meta_payload(f"/new {project_name}")
        )
        assert r.status_code == 200

        # Preseed a fresh local lock BEFORE the /p attempt, from
        # the test thread. The /webhook thread will see it when it
        # runs acquire_for_bot during send_prompt. Use a separate
        # connection with the same DB file to avoid the in-flight
        # command_handler connection.
        lock_conn = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        lock_conn.row_factory = sqlite3.Row
        now = datetime.now(UTC)
        SqliteSessionLockRepository(lock_conn).upsert(
            SessionLock(
                project_name=project_name,
                owner=LockOwner.LOCAL,
                acquired_at=now - timedelta(seconds=5),
                last_activity_at=now - timedelta(seconds=5),
            )
        )
        lock_conn.close()

        r = _signed_post(
            client,
            _build_meta_payload(f"/p {project_name} hallo Claude"),
        )
        assert r.status_code == 200

    # The 🔒 hint reached the sender. A 📨 ack did NOT.
    bodies = [body for _, body in sender.sent]
    assert any("🔒 Terminal aktiv" in body for body in bodies)
    assert not any(
        body.startswith("📨 an ") and project_name in body for body in bodies
    )

    # Lock row is still LOCAL — denial must not silently flip it.
    verify_conn = sqlite_repo.connect(str(db_path))
    row = verify_conn.execute(
        "SELECT owner FROM session_locks WHERE project_name = ?",
        (project_name,),
    ).fetchone()
    verify_conn.close()
    assert row is not None
    assert row["owner"] == LockOwner.LOCAL.value


def test_release_clears_local_lock_and_next_prompt_succeeds(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"lock{uuid.uuid4().hex[:4]}"
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

        # Preseed local lock.
        lock_conn = sqlite3.connect(
            str(db_path), isolation_level=None, check_same_thread=False
        )
        lock_conn.row_factory = sqlite3.Row
        now = datetime.now(UTC)
        SqliteSessionLockRepository(lock_conn).upsert(
            SessionLock(
                project_name=project_name,
                owner=LockOwner.LOCAL,
                acquired_at=now - timedelta(seconds=5),
                last_activity_at=now - timedelta(seconds=5),
            )
        )
        lock_conn.close()

        # /release drops the row back to free.
        r = _signed_post(
            client, _build_meta_payload(f"/release {project_name}")
        )
        assert r.status_code == 200

        # Now /p <name> <prompt> must succeed (bot acquires freely).
        r = _signed_post(
            client, _build_meta_payload(f"/p {project_name} ready now")
        )
        assert r.status_code == 200

    bodies = [body for _, body in sender.sent]
    assert any("🔓 Lock" in body and project_name in body for body in bodies)
    assert any("📨 an" in body and project_name in body for body in bodies)
