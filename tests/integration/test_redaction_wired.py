"""Integration test: redaction actually sits between the command
dispatcher and the outgoing MessageSender.

We send a deliberately invalid ``/new <name>`` where the name matches
the AWS access-key format. The error reply echoes the offending name,
so the raw body coming out of the command handler contains the AWS-key
shape. After the pipeline, the recording sender must see the redacted
form — proving the RedactingMessageSender decorator is wired into
``create_app``.
"""

from __future__ import annotations

import hashlib
import hmac
import json

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
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    base[KEY_META_VERIFY_TOKEN] = "irrelevant"
    return _StubSecrets(**base)


def _build_text_payload(text: str) -> bytes:
    """Smallest Meta-shaped webhook body that drives the command router."""
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
                                    "phone_number_id": "PHONE_NUMBER_ID",
                                },
                                "messages": [
                                    {
                                        "from": ALLOWED_SENDER,
                                        "id": "wamid.REDACT_INTEG_01",
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
    response = client.post(
        "/webhook",
        content=body,
        headers={
            "Content-Type": "application/json",
            "X-Hub-Signature-256": sig,
        },
    )
    assert response.status_code == 200


def test_aws_key_shape_in_command_error_is_redacted_on_wire() -> None:
    """The command handler's error reply echoes the invalid project name.
    Our name happens to match AKIA[A-Z0-9]{16}, so stage 1 fires and the
    recording sender sees ``<REDACTED:aws-key>`` instead of the raw token.
    """
    recorder = _Recorder()
    app = create_app(
        Settings(env=Environment.PROD),
        secrets_provider=_secrets(),
        message_sender=recorder,
    )
    client = TestClient(app)

    # AWS access-key format: AKIA + 16 alnum uppercase. Project name
    # validation also fails (uppercase not allowed), so the error reply
    # echoes the offending string back — that's what we want to redact.
    _signed_post(client, _build_text_payload("/new AKIAIOSFODNN7EXAMPLE"))

    assert len(recorder.sent) == 1, recorder.sent
    to, body = recorder.sent[0]
    assert to == ALLOWED_SENDER
    # Proof the decorator was in the path: raw token scrubbed, placeholder present.
    assert "AKIAIOSFODNN7EXAMPLE" not in body
    assert "<REDACTED:aws-key>" in body


def test_clean_reply_passes_through_unchanged() -> None:
    """Negative control: a reply that contains no secrets isn't mutated."""
    recorder = _Recorder()
    app = create_app(
        Settings(env=Environment.PROD),
        secrets_provider=_secrets(),
        message_sender=recorder,
    )
    client = TestClient(app)
    _signed_post(client, _build_text_payload("/ping"))
    assert len(recorder.sent) == 1
    _, body = recorder.sent[0]
    assert body.startswith("pong")
    assert "<REDACTED:" not in body
