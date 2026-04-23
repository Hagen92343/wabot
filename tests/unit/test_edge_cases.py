"""Phase 9 C9.3 — Edge-case hardening.

Covers the four asymmetries the Phase-1-8 builds left behind:

1. Whitespace-only bare prompts must not reach Claude.
2. Non-ASCII / exotic project names yield friendly rejections.
3. Oversized bare prompts → OutputService handles the 10 KB
   dialog (regression guard, the actual pipeline is C3.5).
4. The webhook feeds the sanitised text into the command
   handler (integration-level check that sanitize_inbound_text
   really landed).
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
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    validate_project_name,
)
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import (
    ALL_KEYS,
    KEY_ALLOWED_SENDERS,
    KEY_META_APP_SECRET,
    KEY_META_VERIFY_TOKEN,
    SecretNotFoundError,
)

pytestmark = [pytest.mark.integration]

APP_SECRET = "edge-e2e-secret"
VERIFY_TOKEN = "edge-e2e-verify"
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


def _full_stub() -> StubSecrets:
    base = {key: f"placeholder-{key}" for key in ALL_KEYS}
    base[KEY_META_APP_SECRET] = APP_SECRET
    base[KEY_META_VERIFY_TOKEN] = VERIFY_TOKEN
    base[KEY_ALLOWED_SENDERS] = ALLOWED_SENDER
    return StubSecrets(**base)


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


# ---- 1. project-name validation --------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "alpha beta",  # whitespace
        "αlpha",  # noqa: RUF001 — test targets Greek-alpha rejection
        "🔥fire",  # emoji
        "project.name",  # dot
        "ALPHA",  # uppercase
        "_leading",  # leading underscore
        "a",  # too short
        "a" * 33,  # too long
    ],
)
def test_exotic_project_names_rejected(bad_name: str) -> None:
    with pytest.raises(InvalidProjectNameError):
        validate_project_name(bad_name)


@pytest.mark.parametrize(
    "ok_name",
    ["alpha", "alpha-beta", "alpha_beta", "a1", "project-1_x"],
)
def test_well_formed_names_accepted(ok_name: str) -> None:
    assert validate_project_name(ok_name) == ok_name


# ---- 2. bare prompts (whitespace / empty / huge) ---------------------


def test_empty_bare_prompt_does_not_reach_session_service(bot) -> None:
    app, sender = bot
    with TestClient(app) as client:
        r = _signed_post(client, _payload("   \n  \t  "))
        assert r.status_code == 200

    # The /help fallthrough should answer (phase-1 router) — what
    # matters is that nothing crashed and no 📨 ack appears
    # (there's no active project anyway).
    bodies = [body for _, body in sender.sent]
    assert not any(b.startswith("📨 an ") for b in bodies), bodies


def test_control_chars_stripped_before_router(bot) -> None:
    """Integration check that sanitize_inbound_text lands in the
    pipeline. A /ping preceded by a NUL must still reach the router."""
    app, sender = bot
    with TestClient(app) as client:
        r = _signed_post(client, _payload("\x00/ping"))
        assert r.status_code == 200

    bodies = [body for _, body in sender.sent]
    assert any(body.startswith("pong") for body in bodies), (
        f"sanitized /ping failed to reach router, got {bodies!r}"
    )


def test_escape_byte_stripped_before_router(bot) -> None:
    """The ESC byte itself gets stripped before the router. The
    following ``[2J`` characters stay, so the text becomes
    ``[2J/ping`` — that's a bare prompt, not a command. We only
    guard that no ``\\x1b`` makes it into any outbound body."""
    app, sender = bot
    with TestClient(app) as client:
        r = _signed_post(client, _payload("\x1b[2J/ping"))
        assert r.status_code == 200

    bodies = [body for _, body in sender.sent]
    for body in bodies:
        assert "\x1b" not in body, f"ESC leaked into outbound: {body!r}"


def test_long_bare_prompt_does_not_crash_webhook(bot) -> None:
    """Regression guard: 15 KB of text must not break the pipeline.
    The OutputService dialog (Spec §10 C3.5) is already tested in
    its own module — here we just assert the webhook survives."""
    app, sender = bot
    big = "A" * 15_000
    with TestClient(app) as client:
        r = _signed_post(client, _payload(big))
        assert r.status_code == 200

    # The bot must have produced *some* reply; either the router
    # answered with a help / size-dialog fallthrough. The key
    # invariant is "no crash".
    assert len(sender.sent) >= 1
