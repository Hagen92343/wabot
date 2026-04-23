"""C8.4 integration — GET /metrics exposes real Prometheus series
after /webhook traffic.

Signed /webhook POST → counters + histogram populated; plain GET
/metrics returns the exposition with all expected series."""

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

APP_SECRET = "metrics-e2e-secret"
VERIFY_TOKEN = "metrics-e2e-verify"
ALLOWED_SENDER = "+491701234567"


class _Sender:
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


def _full_stub() -> _StubSecrets:
    base = {key: f"placeholder-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return _StubSecrets(**base)


def _payload(text: str) -> bytes:
    body = {
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
    return json.dumps(body, separators=(",", ":")).encode()


def _signed_post(client: TestClient, body: bytes) -> httpx.Response:
    sig = "sha256=" + hmac.new(APP_SECRET.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )


@pytest.fixture
def bot(tmp_path: Path):
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = _Sender()
    settings = Settings(env=Environment.PROD, log_dir=tmp_path / "logs")
    app = create_app(
        settings=settings,
        secrets_provider=_full_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
    )
    return app, sender


def test_metrics_endpoint_empty_before_traffic(bot) -> None:
    app, _ = bot
    with TestClient(app) as client:
        r = client.get("/metrics")
        assert r.status_code == 200
        # Without any traffic, only the request to /metrics itself may
        # populate the latency histogram — other counters stay zero.
        body = r.text
        # whatsbot_messages_total should not be present yet.
        assert "whatsbot_messages_total" not in body


def test_metrics_exposes_inbound_and_outbound_counters(bot) -> None:
    app, _ = bot
    with TestClient(app) as client:
        # Send a /ping that the default router always answers →
        # 1 inbound + 1 outbound.
        r = _signed_post(client, _payload("/ping"))
        assert r.status_code == 200
        r = _signed_post(client, _payload("/ping"))
        assert r.status_code == 200

        m = client.get("/metrics")
        assert m.status_code == 200

    body = m.text
    assert 'whatsbot_messages_total{direction="in",kind="text"} 2' in body
    assert 'whatsbot_messages_total{direction="out",kind="text"} 2' in body


def test_metrics_exposes_response_latency_histogram(bot) -> None:
    app, _ = bot
    with TestClient(app) as client:
        _signed_post(client, _payload("/ping"))
        m = client.get("/metrics")

    body = m.text
    assert "# TYPE whatsbot_response_latency_seconds histogram" in body
    # At least one webhook latency observation.
    assert 'whatsbot_response_latency_seconds_bucket{le="+Inf"' in body


def test_metrics_endpoint_content_type_is_plain_text(bot) -> None:
    app, _ = bot
    with TestClient(app) as client:
        _signed_post(client, _payload("/ping"))
        m = client.get("/metrics")

    assert m.status_code == 200
    ctype = m.headers.get("content-type", "")
    assert ctype.startswith("text/plain")


def test_rejected_sender_does_not_bump_inbound_counter(bot) -> None:
    app, _ = bot
    with TestClient(app) as client:
        # Valid signature but whitelist-rejected sender → silent 200.
        body = {
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
                                        "from": "+490000000000",
                                        "id": f"wamid.{uuid.uuid4().hex}",
                                        "type": "text",
                                        "text": {"body": "/ping"},
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        }
        raw = json.dumps(body, separators=(",", ":")).encode()
        sig = "sha256=" + hmac.new(
            APP_SECRET.encode(), raw, hashlib.sha256
        ).hexdigest()
        r = client.post(
            "/webhook",
            content=raw,
            headers={
                "Content-Type": "application/json",
                "X-Hub-Signature-256": sig,
            },
        )
        assert r.status_code == 200
        m = client.get("/metrics")

    # Counter must *not* have been bumped — whitelist gate runs before
    # the metrics site.
    assert 'whatsbot_messages_total{direction="in",kind="text"}' not in m.text
