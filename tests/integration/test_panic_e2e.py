"""End-to-end smoke for Phase 6 C6.2 ``/panic`` via /webhook.

Real ``SubprocessTmuxController``, fake process killer (we don't
really want to ``pkill -9 -f safe-claude`` on the developer's
machine!), no notifier. Verifies that:

* All ``wb-*`` tmux sessions are dead afterwards.
* A non-wb session survives.
* YOLO project rows are flipped to Normal.
* ``mode_events`` rows with ``event='panic_reset'`` land per
  YOLO project.
* Lockdown is engaged in ``app_state`` and the touch-file is
  on disk.
* The panic-reply ack lands in the WhatsApp sender.
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
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.domain.projects import Mode, Project, SourceMode
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
    """Records but never actually pkills."""

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


def test_panic_kills_wb_sessions_resets_yolo_engages_lockdown(
    tmp_path: Path, tmux_session_cleanup: list[str]
) -> None:
    """One wb-* tmux session in YOLO mode + one DB-only YOLO project
    (no tmux) + one foreign tmux session. Panic must kill the wb-*,
    reset BOTH YOLOs, leave the foreign session alone.

    NOTE: We intentionally don't ``/p`` the second project — Phase 4
    has a latent ``session_id TEXT UNIQUE`` collision when two fresh
    sessions both have empty session_ids. Out of scope for C6.2.
    """
    name_a = f"p{uuid.uuid4().hex[:6]}"
    name_b = f"p{uuid.uuid4().hex[:6]}"
    foreign = f"foreign{uuid.uuid4().hex[:6]}"
    tmux_session_cleanup.extend([f"wb-{name_a}", f"wb-{name_b}", foreign])

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
        # Project A: brought up via the API, switched to YOLO.
        _signed_post(client, _build_meta_payload(f"/new {name_a}"))
        _signed_post(client, _build_meta_payload(f"/p {name_a}"))
        _signed_post(client, _build_meta_payload("/mode yolo"))
        assert _wait_for_tmux_session(f"wb-{name_a}")

        # Project B: DB-only YOLO project (no tmux). Bypasses the
        # claude_sessions UNIQUE bug, exercises the YOLO-reset path
        # for projects without a live pane.
        SqliteProjectRepository(conn).create(
            Project(
                name=name_b,
                source_mode=SourceMode.EMPTY,
                created_at=datetime.now(UTC),
                mode=Mode.YOLO,
            )
        )

        # A non-bot tmux session that must survive panic.
        subprocess.run(
            ["tmux", "new-session", "-d", "-s", foreign, "sleep", "30"],
            check=True,
        )
        assert _wait_for_tmux_session(foreign)

        # /panic — the moment of truth.
        r = _signed_post(client, _build_meta_payload("/panic"))
        assert r.status_code == 200

    # Reply landed.
    bodies = [body for _, body in sender.sent]
    panic_replies = [b for b in bodies if "🚨" in b and "PANIC" in b]
    assert panic_replies, f"no panic reply in {bodies}"
    assert any("Lockdown" in b for b in panic_replies)
    assert any("/unlock" in b for b in panic_replies)

    # The wb-* session is dead.
    check = subprocess.run(
        ["tmux", "has-session", "-t", f"wb-{name_a}"],
        capture_output=True,
        check=False,
    )
    assert check.returncode != 0, f"wb-{name_a} should be dead"

    # Foreign session survives.
    foreign_check = subprocess.run(
        ["tmux", "has-session", "-t", foreign],
        capture_output=True,
        check=False,
    )
    assert foreign_check.returncode == 0, "non-bot session must survive panic"

    # process_killer was called with the safe-claude pattern.
    assert "safe-claude" in killer.calls

    # Both YOLO projects (name_a + name_b) are now Normal in the DB.
    verify_conn = sqlite_repo.connect(str(db_path))
    for n in (name_a, name_b):
        row = verify_conn.execute(
            "SELECT mode FROM projects WHERE name = ?", (n,)
        ).fetchone()
        assert row is not None, f"project {n} should still exist"
        assert row["mode"] == "normal", f"{n} should be reset to normal"

    # mode_events rows landed for BOTH YOLO resets.
    audit_rows = verify_conn.execute(
        "SELECT project_name FROM mode_events "
        "WHERE event = 'panic_reset' ORDER BY project_name"
    ).fetchall()
    audited = {r["project_name"] for r in audit_rows}
    assert name_a in audited
    assert name_b in audited

    # Lockdown engaged in app_state.
    lockdown_row = verify_conn.execute(
        "SELECT value FROM app_state WHERE key = 'lockdown'"
    ).fetchone()
    assert lockdown_row is not None
    assert '"engaged":true' in lockdown_row["value"]
    verify_conn.close()

    # Touch-file on disk.
    assert panic_marker.exists()
