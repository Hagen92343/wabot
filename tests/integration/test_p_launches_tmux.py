"""End-to-end C4.1d smoke: ``/p <name>`` via /webhook starts a tmux
session and persists a ``claude_sessions`` row.

Skipped when ``tmux`` isn't on ``PATH``. No real Claude Code is
needed: we inject ``safe_claude_binary=/bin/true`` so the command
that lands in the tmux pane exits quietly and we just verify the
session + DB bookkeeping.

The fixture body is built inline rather than as a JSON file so we
can parametrise ``/new alpha`` and ``/p alpha`` without duplicating
fixtures for every command variant.
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
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
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
    """Collect session names and kill them on teardown.

    Individual tests append their session names to the returned list
    so cleanup works even when the test fails before reaching an
    explicit kill.
    """
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            check=False,
        )


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _build_client(
    *, projects_root: Path, db_path: Path
) -> tuple[TestClient, RecordingSender, sqlite3.Connection]:
    sender = RecordingSender()
    secrets = _full_secret_stub()
    # Open a real on-disk DB so post-request state can be observed from the
    # test process via a second connection. TestClient runs handlers in a
    # worker thread, so we need check_same_thread=False for the connection
    # that the app uses; sqlite_repo.connect doesn't expose that flag so
    # we build the connection directly here and apply the schema + PRAGMAs
    # by hand.
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    for stmt in sqlite_repo.PRAGMAS:
        conn.execute(stmt)
    sqlite_repo.apply_schema(conn)

    app = create_app(
        settings=Settings(env=Environment.PROD),
        secrets_provider=secrets,
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
    )
    return TestClient(app), sender, conn


def test_p_starts_tmux_and_populates_claude_sessions_row(
    projects_root: Path,
    db_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"alpha{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

    client, sender, _ = _build_client(
        projects_root=projects_root, db_path=db_path
    )

    # 1) /new <name>
    response = _signed_post(client, _build_meta_payload(f"/new {project_name}"))
    assert response.status_code == 200
    assert any(project_name in body for _, body in sender.sent)

    # 2) /p <name>
    response = _signed_post(client, _build_meta_payload(f"/p {project_name}"))
    assert response.status_code == 200

    # 3) tmux session exists.
    ctrl = SubprocessTmuxController()
    assert ctrl.has_session(f"wb-{project_name}") is True

    # 4) claude_sessions row populated. Open a fresh connection so we
    # see the row the handler committed on the app's own connection.
    check_conn = sqlite_repo.connect(str(db_path))
    try:
        repo = SqliteClaudeSessionRepository(check_conn)
        row = repo.get(project_name)
        assert row is not None
        assert row.project_name == project_name
    finally:
        check_conn.close()


def test_p_is_idempotent_across_two_calls(
    projects_root: Path,
    db_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"beta{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project_name}")

    client, _, _ = _build_client(
        projects_root=projects_root, db_path=db_path
    )

    _signed_post(client, _build_meta_payload(f"/new {project_name}"))
    _signed_post(client, _build_meta_payload(f"/p {project_name}"))
    # Second /p must not fail and must not spawn a duplicate session.
    response = _signed_post(client, _build_meta_payload(f"/p {project_name}"))
    assert response.status_code == 200

    ctrl = SubprocessTmuxController()
    wb_sessions = [
        s for s in ctrl.list_sessions(prefix="wb-") if s == f"wb-{project_name}"
    ]
    assert wb_sessions == [f"wb-{project_name}"]
