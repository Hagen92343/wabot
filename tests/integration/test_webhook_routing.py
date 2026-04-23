"""End-to-end webhook routing tests via FastAPI TestClient.

These exercise the whole stack: signature verification + sender whitelist +
payload extraction + command router + outbound sender. The ``MessageSender``
is replaced with a recording stub so we can assert what *would* be sent.
"""

from __future__ import annotations

import hashlib
import hmac
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
    SecretNotFoundError,
)

pytestmark = pytest.mark.integration

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
APP_SECRET = "test-app-secret"
VERIFY_TOKEN = "test-verify-token"
ALLOWED_SENDER = "+491701234567"


class RecordingSender:
    """In-memory MessageSender for assertions."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


class StubSecrets:
    """SecretsProvider with a fixed in-memory backing dict."""

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


def _full_secret_stub() -> StubSecrets:
    """All 7 spec-§4 keys filled with placeholders, plus the three the
    webhook actually uses populated with the test's expected values."""
    base = {key: f"placeholder-for-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return StubSecrets(**base)


def _make_client(
    secrets: StubSecrets | None = None,
    sender: RecordingSender | None = None,
    env: Environment = Environment.PROD,
) -> tuple[TestClient, RecordingSender]:
    sender = sender if sender is not None else RecordingSender()
    secrets = secrets if secrets is not None else _full_secret_stub()
    app = create_app(Settings(env=env), secrets_provider=secrets, message_sender=sender)
    return TestClient(app), sender


def _signed_post(
    client: TestClient,
    fixture: str,
    *,
    secret: str = APP_SECRET,
    tamper: bool = False,
) -> object:
    body = (FIXTURES / f"{fixture}.json").read_bytes()
    signed_body = body if not tamper else body + b" "
    sig = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return client.post(
        "/webhook",
        content=signed_body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )


# --- GET /webhook (subscribe handshake) ------------------------------------


def test_subscribe_challenge_returns_challenge_on_match() -> None:
    client, _ = _make_client()
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": VERIFY_TOKEN,
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.text == "12345"


def test_subscribe_challenge_rejects_wrong_token() -> None:
    client, _ = _make_client()
    response = client.get(
        "/webhook",
        params={
            "hub.mode": "subscribe",
            "hub.verify_token": "WRONG",
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 403


def test_subscribe_challenge_rejects_no_params() -> None:
    client, _ = _make_client()
    response = client.get("/webhook")
    assert response.status_code == 403


# --- POST /webhook routing -------------------------------------------------


def test_ping_fixture_routes_to_pong_reply() -> None:
    client, sender = _make_client()
    response = _signed_post(client, "meta_ping")
    assert response.status_code == 200
    assert len(sender.sent) == 1
    to, body = sender.sent[0]
    assert to == ALLOWED_SENDER
    assert "pong" in body and "v0.1.0" in body


def test_status_fixture_routes_to_status_reply() -> None:
    client, sender = _make_client()
    response = _signed_post(client, "meta_status")
    assert response.status_code == 200
    assert len(sender.sent) == 1
    body = sender.sent[0][1]
    assert "whatsbot" in body
    assert "uptime" in body
    assert "env" in body and "prod" in body


def test_help_fixture_routes_to_help_reply() -> None:
    client, sender = _make_client()
    response = _signed_post(client, "meta_help")
    assert response.status_code == 200
    body = sender.sent[0][1]
    for cmd in ("/ping", "/status", "/help"):
        assert cmd in body


def test_unknown_command_still_replies_with_hint() -> None:
    """Phase 4 changes the semantics: bare (non-slash) text is now a
    prompt intended for the active project, not an "unknown command".
    When no project is active, the reply hints at ``/p <name>``
    rather than at ``/help``."""
    client, sender = _make_client()
    response = _signed_post(client, "meta_unknown_command")
    assert response.status_code == 200
    assert len(sender.sent) == 1
    body = sender.sent[0][1]
    assert "kein aktives Projekt" in body
    assert "/p" in body


def test_unknown_sender_is_silently_dropped() -> None:
    client, sender = _make_client()
    response = _signed_post(client, "meta_unknown_sender")
    # 200 OK + no outbound message — never tell the attacker they're rejected.
    assert response.status_code == 200
    assert sender.sent == []


def test_image_without_active_project_prompts_user_to_set_one() -> None:
    # Phase 7 C7.1 — images are now routed through MediaService. When
    # no project is active, the bot replies with a hint rather than
    # silently dropping (Spec §9 requires friendly reject replies on
    # media so the sender isn't left wondering).
    client, sender = _make_client()
    response = _signed_post(client, "meta_non_text")
    assert response.status_code == 200
    assert len(sender.sent) == 1
    to, body = sender.sent[0]
    assert to == ALLOWED_SENDER
    assert "/p" in body  # hint to set an active project


def test_invalid_signature_silently_drops_request() -> None:
    client, sender = _make_client()
    response = _signed_post(client, "meta_ping", tamper=True)
    # Tampered body invalidates the signature → 200 + no routing.
    assert response.status_code == 200
    assert sender.sent == []


def test_wrong_secret_signature_silently_drops_request() -> None:
    client, sender = _make_client()
    response = _signed_post(client, "meta_ping", secret="WRONG-SECRET")
    assert response.status_code == 200
    assert sender.sent == []


def test_missing_signature_header_silently_drops_request() -> None:
    client, sender = _make_client()
    body = (FIXTURES / "meta_ping.json").read_bytes()
    response = client.post(
        "/webhook",
        content=body,
        headers={"Content-Type": "application/json"},
    )
    assert response.status_code == 200
    assert sender.sent == []


def test_malformed_json_payload_silently_drops_request() -> None:
    client, sender = _make_client()
    body = b"not valid json {{{{"
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
    assert sender.sent == []


# --- Dev-mode shortcut -----------------------------------------------------


def test_dev_mode_without_app_secret_skips_signature_check() -> None:
    """In dev with no app secret in Keychain, signature is not enforced."""
    client, sender = _make_client(
        secrets=StubSecrets(**{KEY_ALLOWED_SENDERS: ALLOWED_SENDER}),
        env=Environment.DEV,
    )
    body = (FIXTURES / "meta_ping.json").read_bytes()
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": "sha256=garbage",
        },
    )
    assert response.status_code == 200
    assert len(sender.sent) == 1


# --- Constant-time padding -------------------------------------------------


def test_webhook_response_is_padded_for_constant_time() -> None:
    """ConstantTimeMiddleware runs on /webhook even on rejection paths."""
    import time as _time

    client, _ = _make_client()
    start = _time.perf_counter()
    response = _signed_post(client, "meta_unknown_sender")
    elapsed = _time.perf_counter() - start
    assert response.status_code == 200
    # Default min_duration_ms in main.py is 200ms; allow some slack.
    assert elapsed >= 0.18, f"webhook returned in only {elapsed * 1000:.0f}ms"


def test_health_endpoint_is_not_padded() -> None:
    """Sanity check: ConstantTime is path-scoped to /webhook."""
    import time as _time

    client, _ = _make_client()
    start = _time.perf_counter()
    response = client.get("/health")
    elapsed = _time.perf_counter() - start
    assert response.status_code == 200
    assert elapsed < 0.18, f"/health was padded ({elapsed * 1000:.0f}ms)"
