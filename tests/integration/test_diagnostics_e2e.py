"""C8.2 end-to-end test — /log, /errors, /ps via signed /webhook,
with a hand-written ``app.jsonl`` fixture under ``tmp_path``.

No tmux required: /ps returns "keine aktiven Sessions." because
there's no SubprocessTmuxController wired. That still exercises the
command-handler → DiagnosticsService → FileLogReader chain.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.config import Environment, Settings
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    SecretNotFoundError,
)

pytestmark = [pytest.mark.integration]

APP_SECRET = "diag-test-app-secret"
VERIFY_TOKEN = "diag-test-verify-token"
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


def _seed_app_log(log_dir: Path, entries: list[dict[str, object]]) -> None:
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / "app.jsonl").open("w", encoding="utf-8") as fh:
        for entry in entries:
            fh.write(json.dumps(entry) + "\n")


@pytest.fixture
def bot_app(tmp_path: Path):
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    log_dir = tmp_path / "logs"
    # A small canned log — one success trace + one unrelated event
    # + one error.
    _seed_app_log(
        log_dir,
        [
            {
                "ts": "2026-04-21T14:32:11.000Z",
                "level": "INFO",
                "logger": "whatsbot.webhook",
                "event": "webhook_in",
                "msg_id": "mTRACE",
                "project": "alpha",
            },
            {
                "ts": "2026-04-21T14:32:11.100Z",
                "level": "INFO",
                "logger": "whatsbot.router",
                "event": "command_routed",
                "msg_id": "mTRACE",
                "command": "/ls",
            },
            {
                "ts": "2026-04-21T14:32:12.000Z",
                "level": "WARNING",
                "logger": "whatsbot.hook",
                "event": "deny_pattern_matched",
                "msg_id": "mOTHER",
            },
            {
                "ts": "2026-04-21T14:32:13.000Z",
                "level": "ERROR",
                "logger": "whatsbot.meta",
                "event": "send_failed",
                "msg_id": "mOTHER",
            },
            # Invalid line to confirm we don't crash on garbage.
            "not-json-at-all",
        ][:-1]
        + [],
    )
    # Append a garbage line by hand.
    with (log_dir / "app.jsonl").open("a", encoding="utf-8") as fh:
        fh.write("not-json-at-all\n")

    sender = RecordingSender()
    settings = Settings(env=Environment.PROD, log_dir=log_dir)
    app = create_app(
        settings=settings,
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
    )
    return app, sender


def test_log_msg_id_renders_trace_from_app_jsonl(bot_app) -> None:
    app, sender = bot_app
    with TestClient(app) as client:
        r = _signed_post(client, _build_payload("/log mTRACE"))
        assert r.status_code == 200

    bodies = [b for _, b in sender.sent]
    matches = [b for b in bodies if "Trace msg_id=mTRACE" in b]
    assert matches, f"trace reply missing; got {bodies}"
    body = matches[0]
    assert "webhook_in" in body
    assert "command_routed" in body
    # Only mTRACE entries; mOTHER never matched.
    assert "send_failed" not in body


def test_errors_lists_warning_and_error_events(bot_app) -> None:
    app, sender = bot_app
    with TestClient(app) as client:
        r = _signed_post(client, _build_payload("/errors"))
        assert r.status_code == 200

    bodies = [b for _, b in sender.sent]
    matches = [b for b in bodies if "Fehler" in b]
    assert matches, f"/errors reply missing; got {bodies}"
    body = matches[0]
    assert "deny_pattern_matched" in body
    assert "send_failed" in body
    # INFO-level event must not surface under /errors.
    assert "command_routed" not in body


def test_ps_returns_empty_snapshot_without_sessions(bot_app) -> None:
    app, sender = bot_app
    with TestClient(app) as client:
        r = _signed_post(client, _build_payload("/ps"))
        assert r.status_code == 200

    bodies = [b for _, b in sender.sent]
    assert any("keine aktiven" in b for b in bodies), bodies


def test_update_returns_manual_procedure_hint(bot_app) -> None:
    app, sender = bot_app
    with TestClient(app) as client:
        r = _signed_post(client, _build_payload("/update"))
        assert r.status_code == 200

    bodies = [b for _, b in sender.sent]
    assert any("manuell" in b for b in bodies), bodies


def test_log_without_args_returns_usage_hint(bot_app) -> None:
    app, sender = bot_app
    with TestClient(app) as client:
        r = _signed_post(client, _build_payload("/log"))
        assert r.status_code == 200

    bodies = [b for _, b in sender.sent]
    assert any("Verwendung" in b and "<msg_id>" in b for b in bodies), bodies
