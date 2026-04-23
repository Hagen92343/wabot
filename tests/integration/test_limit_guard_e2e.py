"""C8.1 — end-to-end test that a preseeded active max_limits row
blocks ``/p <name> <prompt>`` with a friendly ``⏸ Max-Limit`` reply.

Signed /webhook, real TmuxController (so ensure_started is genuine),
safe-claude replaced with /bin/true.
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
from whatsbot.adapters.sqlite_max_limits_repository import (
    SqliteMaxLimitsRepository,
)
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.domain.limits import LimitKind, MaxLimit
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


def _build_payload(text: str) -> bytes:
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WABA",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "messages": [
                                {
                                    "from": ALLOWED_SENDER,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "type": "text",
                                    "text": {"body": text},
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
def tmux_cleanup() -> Iterator[list[str]]:
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            check=False,
        )


def test_prompt_rejected_with_reset_hint_during_active_limit(
    tmp_path: Path,
    tmux_cleanup: list[str],
) -> None:
    project = f"lim{uuid.uuid4().hex[:4]}"
    tmux_cleanup.append(f"wb-{project}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    # Preseed an active session-5h limit that resets in ~3 hours.
    reset_at = int(time.time()) + 3 * 3600 + 22 * 60  # 3h 22m
    SqliteMaxLimitsRepository(conn).upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=reset_at,
            remaining_pct=0.50,
        )
    )

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
        r = _signed_post(client, _build_payload(f"/new {project}"))
        assert r.status_code == 200
        r = _signed_post(
            client, _build_payload(f"/p {project} hallo Claude")
        )
        assert r.status_code == 200

    # The ⏸ Max-Limit reply reached the sender. No 📨 ack because
    # send_prompt aborted *before* tmux.send_keys.
    bodies = [body for _, body in sender.sent]
    assert any(
        "⏸ Max-Limit erreicht" in body
        and "session_5h" in body
        and ("3h 22m" in body or "3h 21m" in body)  # clock-skew tolerance
        for body in bodies
    )
    assert not any(
        body.startswith("📨 an ") and project in body for body in bodies
    )


def test_prompt_reaches_tmux_when_limit_expired(
    tmp_path: Path,
    tmux_cleanup: list[str],
) -> None:
    """Sanity: an already-expired limit row must not block."""
    project = f"ok{uuid.uuid4().hex[:4]}"
    tmux_cleanup.append(f"wb-{project}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    # A long-expired row — should be treated as inactive.
    SqliteMaxLimitsRepository(conn).upsert(
        MaxLimit(
            kind=LimitKind.SESSION_5H,
            reset_at_ts=int(time.time()) - 3600,
        )
    )

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
        r = _signed_post(client, _build_payload(f"/new {project}"))
        assert r.status_code == 200
        r = _signed_post(client, _build_payload(f"/p {project} hi"))
        assert r.status_code == 200

    bodies = [body for _, body in sender.sent]
    # 📨 ack fired → limit guard was not triggered.
    assert any(
        body.startswith("📨 an ") and project in body for body in bodies
    )
    assert not any("⏸ Max-Limit" in body for body in bodies)
