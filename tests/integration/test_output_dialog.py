"""Integration test for the /send · /discard · /save dialog.

The Meta webhook intercepts these three commands *before* the regular
router and routes them to OutputService. We pre-seed a pending row +
on-disk file via the output-service directly, then POST the command
through a signed webhook and verify the RecordingSender sees the
chunks / ack message.

No-pending cases (all three return "Kein wartender Output.") are also
exercised.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.config import Environment, Settings
from whatsbot.domain.output_guard import THRESHOLD_BYTES
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    SecretNotFoundError,
)

pytestmark = pytest.mark.integration

APP_SECRET = "test-app-secret"
ALLOWED_SENDER = "+491701234567"


class _Recorder:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


class _StubSecrets:
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


def _secrets() -> _StubSecrets:
    base = {key: f"placeholder-for-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = "irrelevant"
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return _StubSecrets(**base)


def _payload(text: str) -> bytes:
    return json.dumps(
        {
            "object": "whatsapp_business_account",
            "entry": [
                {
                    "id": "acct",
                    "changes": [
                        {
                            "field": "messages",
                            "value": {
                                "messaging_product": "whatsapp",
                                "metadata": {
                                    "display_phone_number": "+491700000000",
                                    "phone_number_id": "ID",
                                },
                                "messages": [
                                    {
                                        "from": ALLOWED_SENDER,
                                        "id": f"wamid.OUTPUT_{text.replace('/', '')}",
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
    ).encode("utf-8")


def _signed_post(client: TestClient, body: bytes) -> None:
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    r = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json", "X-Hub-Signature-256": sig},
    )
    assert r.status_code == 200


def _client(
    tmp_path: Path,
) -> tuple[TestClient, _Recorder, Settings, sqlite3.Connection]:
    """Build an app bound to tmp_path + a thread-safe DB connection.

    FastAPI's ``TestClient`` dispatches each request on a worker
    thread. The default SQLite connection is thread-local; we use
    ``check_same_thread=False`` so the same conn can serve both
    seeding and the TestClient's request thread.
    """
    settings = Settings(
        env=Environment.PROD,
        db_path=tmp_path / "state.db",
        backup_dir=tmp_path / "backups",
        log_dir=tmp_path / "logs",
    )
    conn = sqlite3.connect(
        str(settings.db_path), isolation_level=None, check_same_thread=False
    )
    conn.row_factory = sqlite3.Row
    sqlite_repo.apply_schema(conn)
    recorder = _Recorder()
    app = create_app(
        settings,
        secrets_provider=_secrets(),
        message_sender=recorder,
        db_connection=conn,
        projects_root=tmp_path / "projekte",
    )
    return TestClient(app), recorder, settings, conn


def _seed_pending_output(
    conn: sqlite3.Connection, settings: Settings, body: str
) -> Path:
    """Pre-populate a pending_outputs row + on-disk file using the app's
    own connection so the webhook sees the row immediately."""
    from whatsbot.adapters.sqlite_pending_output_repository import (
        SqlitePendingOutputRepository,
    )
    from whatsbot.domain.pending_outputs import PendingOutput, compute_deadline

    repo = SqlitePendingOutputRepository(conn)
    outputs_dir = settings.db_path.parent / "outputs"
    outputs_dir.mkdir(parents=True, exist_ok=True)
    path = outputs_dir / "seeded.md"
    path.write_text(body, encoding="utf-8")
    repo.create(
        PendingOutput(
            msg_id="seeded",
            project_name="_bot",
            output_path=str(path),
            size_bytes=len(body.encode("utf-8")),
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            deadline_ts=compute_deadline(int(datetime.now(UTC).timestamp())),
        )
    )
    return path


# ---- /send path --------------------------------------------------------


def test_send_delivers_chunks_and_clears_row(tmp_path: Path) -> None:
    client, recorder, settings, conn = _client(tmp_path)
    body = "B" * (THRESHOLD_BYTES * 3)  # forces 3+ chunks through the chunker
    path = _seed_pending_output(conn, settings, body)

    _signed_post(client, _payload("/send"))

    # Recorder saw at least one chunk message plus the final ack.
    assert len(recorder.sent) >= 2
    # Last message is the ack.
    _, ack = recorder.sent[-1]
    assert "Gesendet" in ack
    # File cleaned up after send.
    assert not path.exists()


# ---- /discard path ----------------------------------------------------


def test_discard_drops_row_and_file(tmp_path: Path) -> None:
    client, recorder, settings, conn = _client(tmp_path)
    body = "x" * (THRESHOLD_BYTES + 100)
    path = _seed_pending_output(conn, settings, body)
    assert path.exists()

    _signed_post(client, _payload("/discard"))

    _, ack = recorder.sent[-1]
    assert "Verworfen" in ack
    assert not path.exists()


# ---- /save path -------------------------------------------------------


def test_save_drops_row_but_keeps_file(tmp_path: Path) -> None:
    client, recorder, settings, conn = _client(tmp_path)
    body = "y" * (THRESHOLD_BYTES + 100)
    path = _seed_pending_output(conn, settings, body)

    _signed_post(client, _payload("/save"))

    _, ack = recorder.sent[-1]
    assert "Gespeichert" in ack
    assert path.exists()


# ---- no-pending cases -------------------------------------------------


@pytest.mark.parametrize("command", ["/send", "/discard", "/save"])
def test_resolve_with_no_pending_replies_cleanly(
    tmp_path: Path, command: str
) -> None:
    client, recorder, _settings, _conn = _client(tmp_path)
    _signed_post(client, _payload(command))
    assert len(recorder.sent) == 1
    _, ack = recorder.sent[0]
    assert "Kein wartender Output" in ack
