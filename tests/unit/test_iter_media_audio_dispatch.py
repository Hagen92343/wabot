"""C7.4 — webhook audio dispatch + sofort-ack tests.

Unit-scope: the webhook _dispatch_media helper routes AUDIO through
MediaService.process_audio (not process_unsupported) and the POST
handler sends "🎙 Transkribiere…" before the pipeline runs. These
tests don't need tmux — they exercise the dispatcher via direct
TestClient calls with a stub MediaService.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.application.media_service import MediaOutcome
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


def _audio_payload(*, media_id: str, mime: str = "audio/ogg") -> bytes:
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
                                    "type": "audio",
                                    "audio": {
                                        "id": media_id,
                                        "mime_type": mime,
                                    },
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def _signed_post(client: TestClient, body: bytes) -> object:
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


class StubMediaService:
    """Stand-in for MediaService used only to capture the call.

    The real MediaService needs tmux + Claude wired up for a
    meaningful response; for this dispatcher-level test we just need
    to prove process_audio was called with the right arguments and
    its reply is shipped after the ack.
    """

    def __init__(self) -> None:
        self.audio_calls: list[dict[str, object]] = []

    def process_audio(
        self, *, media_id: str, mime: str | None, sender: str
    ) -> MediaOutcome:
        self.audio_calls.append(
            {"media_id": media_id, "mime": mime, "sender": sender}
        )
        return MediaOutcome(
            kind="sent",
            reply=f"📨 Voice an 'alpha' gesendet.",
            project="alpha",
        )

    # The other process_* methods aren't exercised here but the
    # MediaService protocol requires them — stub with not-implemented
    # so a stray call is loud.
    def process_image(self, **_: object) -> MediaOutcome:  # pragma: no cover
        raise NotImplementedError

    def process_pdf(self, **_: object) -> MediaOutcome:  # pragma: no cover
        raise NotImplementedError

    def process_unsupported(self, kind: object) -> MediaOutcome:  # pragma: no cover
        raise NotImplementedError


def test_audio_webhook_sends_ack_then_reply(tmp_path: Path) -> None:
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = RecordingSender()
    # Skip tmux (audio pipeline doesn't need it at this level — the
    # stub short-circuits process_audio). We build the app in TEST
    # env so MediaService construction doesn't require a real
    # access token, then override the app.state.media_service.
    app = create_app(
        settings=Settings(env=Environment.TEST),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
    )
    stub = StubMediaService()
    app.state.media_service = stub
    # Rebuild the webhook router so it picks up the stub. The
    # factory takes media_service at build time, so we reach in and
    # patch the outer closure by re-including a fresh router.
    from whatsbot.http.meta_webhook import build_router

    app.router.routes = [
        r for r in app.router.routes
        if not (hasattr(r, "path") and r.path == "/webhook")
    ]
    app.include_router(
        build_router(
            settings=Settings(env=Environment.TEST),
            secrets=_full_secret_stub(),
            sender=sender,  # type: ignore[arg-type]
            command_handler=app.state.project_repo  # dummy — won't be hit
            if False
            else _NoopHandler(),  # type: ignore[arg-type]
            media_service=stub,  # type: ignore[arg-type]
        )
    )

    with TestClient(app) as client:
        r = _signed_post(client, _audio_payload(media_id="VN_1"))
        assert r.status_code == 200  # type: ignore[attr-defined]

    # The stub received the right args.
    assert len(stub.audio_calls) == 1
    assert stub.audio_calls[0]["media_id"] == "VN_1"
    assert stub.audio_calls[0]["mime"] == "audio/ogg"
    assert stub.audio_calls[0]["sender"] == ALLOWED_SENDER

    # Two messages went out — ack first, final second, in order.
    assert len(sender.sent) == 2
    (to_ack, body_ack), (to_final, body_final) = sender.sent
    assert to_ack == ALLOWED_SENDER
    assert "Transkribiere" in body_ack
    assert to_final == ALLOWED_SENDER
    assert "📨" in body_final
    assert "Voice" in body_final


class _NoopHandler:
    """Satisfies CommandHandler's type for build_router."""

    def handle(self, text: str) -> object:  # pragma: no cover
        raise NotImplementedError
