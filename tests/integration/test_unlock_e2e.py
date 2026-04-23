"""Phase 6 C6.6 — full /panic → blocked → /unlock → recover via /webhook.

Real ``SubprocessTmuxController``, fake process killer (we don't
actually pkill on the dev machine). Verifies the complete user-facing
flow:

1. /panic engages lockdown — wb-* tmux gone, marker on disk.
2. /ls + /p + bare prompts come back blocked with the lockdown hint.
3. /unlock with wrong PIN fails, lockdown stays.
4. /unlock with correct PIN clears it.
5. After /unlock, /ls works again.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.main import create_app
from whatsbot.ports.process_killer import KillResult
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


class FakeProcessKiller:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def kill_by_pattern(self, pattern: str) -> KillResult:
        self.calls.append(pattern)
        return KillResult(pattern=pattern, exit_code=1, matched_count=0)


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


def test_panic_unlock_full_flow(
    tmp_path: Path, tmux_session_cleanup: list[str]
) -> None:
    project_name = f"u{uuid.uuid4().hex[:6]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = RecordingSender()
    killer = FakeProcessKiller()
    panic_marker = tmp_path / "PANIC"
    settings = Settings(
        env=Environment.PROD,
        panic_marker_path=panic_marker,
    )
    app = create_app(
        settings=settings,
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        process_killer=killer,
    )

    with TestClient(app) as client:
        # Bring up a real tmux session so /panic has something to kill.
        _signed_post(client, _build_meta_payload(f"/new {project_name}"))
        _signed_post(client, _build_meta_payload(f"/p {project_name}"))
        assert _wait_for_tmux_session(f"wb-{project_name}")

        # Step 1: /panic — engages lockdown.
        _signed_post(client, _build_meta_payload("/panic"))
        assert panic_marker.exists()

        # Step 2: arbitrary commands are blocked with the lockdown hint.
        sent_before = len(sender.sent)
        for blocked in ("/ls", f"/p {project_name}", "bare prompt"):
            _signed_post(client, _build_meta_payload(blocked))
        new_replies = [
            body for _, body in sender.sent[sent_before:]
        ]
        assert len(new_replies) == 3
        for reply in new_replies:
            assert "🔒" in reply
            assert "Lockdown" in reply

        # Step 3: wrong PIN keeps lockdown.
        sent_before = len(sender.sent)
        _signed_post(client, _build_meta_payload("/unlock 9999"))
        wrong_replies = sender.sent[sent_before:]
        assert any("Falsche PIN" in body for _, body in wrong_replies)
        assert panic_marker.exists()

        # Step 4: correct PIN clears lockdown.
        sent_before = len(sender.sent)
        _signed_post(client, _build_meta_payload(f"/unlock {PANIC_PIN}"))
        unlock_replies = sender.sent[sent_before:]
        assert any("🔓" in body for _, body in unlock_replies)
        assert any("aufgehoben" in body for _, body in unlock_replies)
        assert not panic_marker.exists()

        # Step 5: post-unlock /ls works again.
        sent_before = len(sender.sent)
        r = _signed_post(client, _build_meta_payload("/ls"))
        assert r.status_code == 200
        post_replies = [body for _, body in sender.sent[sent_before:]]
        # Whatever /ls returns, it must NOT be the lockdown hint.
        assert post_replies, "expected /ls reply after unlock"
        for reply in post_replies:
            assert "🔒" not in reply

    # Lockdown row in DB shows engaged=false at the end.
    verify_conn = sqlite_repo.connect(str(db_path))
    row = verify_conn.execute(
        "SELECT value FROM app_state WHERE key = 'lockdown'"
    ).fetchone()
    verify_conn.close()
    assert row is not None
    assert '"engaged":false' in row["value"]
