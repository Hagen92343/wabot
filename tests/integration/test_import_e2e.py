"""End-to-end test for Phase 11 ``/import`` via signed /webhook.

Exercises the full stack: Meta-signature verification → sender whitelist
→ command router → ProjectService.import_existing → SqliteProjectRepository
→ /ls rendering → /rm flow preserving the imported directory.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whatsbot.config import Environment, Settings
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    KEY_PANIC_PIN,
    SecretNotFoundError,
)

pytestmark = pytest.mark.integration


APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ALLOWED_SENDER = "+491701234567"
TEST_PIN = "1234"


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
            raise SecretNotFoundError(f"stub has no {key!r}")
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


def _stub_secrets() -> StubSecrets:
    base = {key: f"placeholder-for-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    base[KEY_PANIC_PIN] = TEST_PIN
    return StubSecrets(**base)


def _make_payload(text: str, msg_id: str = "wamid.IMPORT_E2E") -> bytes:
    """Build a Meta-shaped JSON payload for a single WhatsApp message."""
    payload = {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "id": "WHATSAPP_BUSINESS_ACCOUNT_ID",
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "messaging_product": "whatsapp",
                            "metadata": {
                                "display_phone_number": "+491700000000",
                                "phone_number_id": "PHONE_NUMBER_ID",
                            },
                            "contacts": [
                                {
                                    "profile": {"name": "Hagen"},
                                    "wa_id": ALLOWED_SENDER.lstrip("+"),
                                }
                            ],
                            "messages": [
                                {
                                    "from": ALLOWED_SENDER,
                                    "id": msg_id,
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
    return json.dumps(payload).encode("utf-8")


def _signed_post(
    client: TestClient,
    text: str,
    *,
    msg_id: str = "wamid.IMPORT_E2E",
) -> None:
    body = _make_payload(text, msg_id=msg_id)
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert response.status_code == 200


def _make_client(
    tmp_path: Path,
) -> tuple[TestClient, RecordingSender]:
    sender = RecordingSender()
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    settings = Settings(
        env=Environment.TEST,
        db_path=tmp_path / "state.db",
        log_dir=tmp_path / "logs",
    )
    app = create_app(
        settings=settings,
        secrets_provider=_stub_secrets(),
        message_sender=sender,
        projects_root=projects_root,
    )
    return TestClient(app), sender


def test_import_existing_end_to_end(tmp_path: Path) -> None:
    # Given: an existing directory outside projects_root that we want to import.
    target = tmp_path / "existing_repo"
    target.mkdir()
    (target / "package.json").write_text('{"name":"demo"}', encoding="utf-8")

    client, sender = _make_client(tmp_path)
    try:
        _signed_post(client, f"/import wabot {target}")
        assert len(sender.sent) == 1
        _, reply = sender.sent[-1]
        assert "✅" in reply
        assert "wabot" in reply
        # Path may be redaction-scrubbed in tests (tmp_path is high-entropy)
        # — assert the key markers instead of the literal path.
        assert "Pfad:" in reply
        # Rule-Vorschlaege aus package.json landen im Reply.
        assert "Rule-Vorschläge" in reply

        # When the user asks /ls next, the imported project shows up.
        _signed_post(client, "/ls", msg_id="wamid.IMPORT_LS")
        _, ls_reply = sender.sent[-1]
        assert "wabot" in ls_reply
        assert "(imported)" in ls_reply

        # Artefakte wurden im Ziel-Ordner angelegt.
        assert (target / "CLAUDE.md").is_file()
        assert (target / ".claudeignore").is_file()
        assert (target / ".whatsbot" / "config.json").is_file()
        assert (target / ".whatsbot" / "suggested-rules.json").is_file()

        # /rm Pending + Confirm für imported Projekt: DB-Row weg, Ordner bleibt.
        _signed_post(client, "/rm wabot", msg_id="wamid.IMPORT_RM1")
        _, rm_reply = sender.sent[-1]
        assert "/rm wabot" in rm_reply or "Bestaetige" in rm_reply or "PIN" in rm_reply.lower()

        _signed_post(client, f"/rm wabot {TEST_PIN}", msg_id="wamid.IMPORT_RM2")
        _, confirm_reply = sender.sent[-1]
        assert "entregistriert" in confirm_reply
        assert "Ordner unberührt" in confirm_reply or "unberuehrt" in confirm_reply

        # Ordner ist noch da (wurde NICHT in den Trash verschoben).
        assert target.is_dir()
        assert (target / "package.json").is_file()
        # Projekt ist aus /ls verschwunden.
        _signed_post(client, "/ls", msg_id="wamid.IMPORT_LS_AFTER")
        _, ls_after = sender.sent[-1]
        assert "wabot" not in ls_after
    finally:
        client.close()


def test_import_rejects_nonexistent_path(tmp_path: Path) -> None:
    client, sender = _make_client(tmp_path)
    try:
        _signed_post(
            client, "/import foo /definitely/does/not/exist/ever",
        )
        _, reply = sender.sent[-1]
        assert "⚠️" in reply
        assert "existiert nicht" in reply
    finally:
        client.close()


def test_import_rejects_relative_path(tmp_path: Path) -> None:
    client, sender = _make_client(tmp_path)
    try:
        _signed_post(client, "/import foo relative/path")
        _, reply = sender.sent[-1]
        assert "⚠️" in reply
        assert "absolut" in reply
    finally:
        client.close()
