"""C9.1 — End-to-End smoke against the full FastAPI stack.

Runs a 9-step user journey through a signed /webhook, a whitelist-
rejecting path, a bad-signature path, and ends with a /metrics
scrape. No Claude subprocess — Phase 4 C4.2-C4.7 exercise the
real ``safe-claude`` spawn path; here we stay on the command-
router level.

Invoke via::

    make smoke

which runs ``pytest -m smoke tests/smoke.py``. We do NOT mark the
test with ``@pytest.mark.integration`` because the filter in
``Makefile`` currently only looks at ``-m smoke``.
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

pytestmark = [pytest.mark.smoke]


APP_SECRET = "smoke-e2e-secret"
VERIFY_TOKEN = "smoke-e2e-verify"
ALLOWED_SENDER = "+491701234567"
REJECTED_SENDER = "+490000000000"

# A canned AWS access-key-shaped string — Spec §10 Stage 1 redaction
# must replace this with ``<REDACTED:aws-key>`` before it hits any
# outbound send.
AWS_FAKE_KEY = "AKIAIOSFODNN7SMOKE00"  # 20 chars, matches AKIA[A-Z0-9]{16}


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


def _full_stub() -> StubSecrets:
    base = {key: f"placeholder-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return StubSecrets(**base)


def _payload(text: str, *, sender: str = ALLOWED_SENDER) -> bytes:
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
                                    "from": sender,
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


def _signed(body: bytes) -> dict[str, str]:
    sig = "sha256=" + hmac.new(
        APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": sig,
    }


def _post(client: TestClient, body: bytes, *, sign: bool = True) -> httpx.Response:
    headers = _signed(body) if sign else {
        "Content-Type": "application/json",
        "X-Hub-Signature-256": "sha256=deadbeef",
    }
    return client.post("/webhook", content=body, headers=headers)


@pytest.fixture
def bot(tmp_path: Path):
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = RecordingSender()
    settings = Settings(env=Environment.PROD, log_dir=tmp_path / "logs")
    app = create_app(
        settings=settings,
        secrets_provider=_full_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
    )
    return app, sender


def test_smoke_end_to_end_journey(bot) -> None:
    """The 9-step journey from the phase-9 plan, as one atomic test.

    The test is intentionally single-scope — a single failure tells
    us which step broke; partial passes are easier to reason about
    than multi-test cross-dependencies.
    """
    app, sender = bot

    with TestClient(app) as client:
        # 1. /ping → pong + footer.
        r = _post(client, _payload("/ping"))
        assert r.status_code == 200

        # 2. /new alpha → project created.
        r = _post(client, _payload("/new alpha"))
        assert r.status_code == 200

        # 3. /ls → listing contains alpha.
        r = _post(client, _payload("/ls"))
        assert r.status_code == 200

        # 4. /mode → current-mode hint (no active project set via /p).
        r = _post(client, _payload("/mode"))
        assert r.status_code == 200

        # 5. Bad signature → 200 OK + silent drop (no reply sent).
        bad_body = _payload("/ping")
        before_bad = len(sender.sent)
        r = _post(client, bad_body, sign=False)
        assert r.status_code == 200
        assert len(sender.sent) == before_bad, (
            "bad-signature request must not produce an outbound reply"
        )

        # 6. Non-whitelisted sender → 200 OK + silent drop.
        before_reject = len(sender.sent)
        rej_body = _payload("/ping", sender=REJECTED_SENDER)
        r = _post(client, rej_body)
        assert r.status_code == 200
        assert len(sender.sent) == before_reject, (
            "non-whitelisted sender must not produce an outbound reply"
        )

        # 7. /status still works after those rejects (bot is alive).
        r = _post(client, _payload("/status"))
        assert r.status_code == 200

        # 8. AWS-key-containing prompt → outbound reply must not
        #    contain the raw key (Spec §10 Stage-1 redaction).
        key_text = f"nebenbei mein backup-key: {AWS_FAKE_KEY}, bitte merken"
        before_key = len(sender.sent)
        r = _post(client, _payload(key_text))
        assert r.status_code == 200
        new_replies = sender.sent[before_key:]
        assert new_replies, "there must be some outbound reply to a bare prompt"
        for _to, body in new_replies:
            assert AWS_FAKE_KEY not in body, (
                f"raw AWS key leaked into outbound reply: {body!r}"
            )

        # 9. /metrics exposes inbound + outbound counters populated.
        m = client.get("/metrics")
        assert m.status_code == 200
        metrics_body = m.text

    # Assertions derived from the 9 steps above — checked once at the
    # end so the test reads like a narrative, not a checklist of
    # individual pytest assertions.
    bodies = [body for _, body in sender.sent]
    joined = "\n".join(bodies)

    # Step 1 landed.
    assert any(body.startswith("pong") for _, body in sender.sent), (
        f"expected a pong reply, got {bodies!r}"
    )
    # Step 2 landed.
    assert any(
        "Projekt 'alpha' angelegt" in body for body in bodies
    ), f"/new reply missing, got {bodies!r}"
    # Step 3 landed — listing lines contain "alpha".
    assert "alpha" in joined, f"/ls did not surface the project, got {bodies!r}"
    # Step 4: mode hint uses a recognisable fragment.
    assert any(
        "Modus" in body or "mode" in body.lower()
        or "normal" in body.lower()
        for body in bodies
    )

    # /metrics accurate — counters match the legitimate-message
    # count. We sent 6 signed + whitelisted messages (steps 1, 2, 3,
    # 4, 7, 8). Bad-sig and rejected-sender steps must NOT bump.
    assert 'whatsbot_messages_total{direction="in",kind="text"} 6' in metrics_body
    # /status path hits the request-latency histogram via the
    # webhook bucket; /metrics scrape itself hits the metrics bucket.
    assert "# TYPE whatsbot_response_latency_seconds histogram" in metrics_body


def test_smoke_metrics_endpoint_content_type(bot) -> None:
    """Guardrail: /metrics must be text/plain so Prometheus scrapes
    parse cleanly (json would crash the default prom expfmt parser)."""
    app, _ = bot
    with TestClient(app) as client:
        r = client.get("/metrics")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("text/plain")


def test_smoke_health_endpoint_reports_alive(bot) -> None:
    """Final smoke check — /health answers with the phase-1 shape."""
    app, _ = bot
    with TestClient(app) as client:
        r = client.get("/health")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["env"] in ("prod", "dev", "test")
    assert "version" in body
    assert isinstance(body["uptime_seconds"], int | float)
