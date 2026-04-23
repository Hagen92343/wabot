"""End-to-end smokes for Phase 7 media pipelines via ``/webhook``.

Covers the full real stack (C7.1 image, C7.2 PDF, unsupported kinds):
  Meta-signed POST → iter_media_messages → MediaService.process_* →
  FakeDownloader → FileMediaCache.store → SessionService.send_prompt →
  tmux send_text → RecordingSender ack reply.

``tmux`` must be present; otherwise the whole spec-§7 Claude-launch
path is stubbed out and these tests would test nothing interesting.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.config import Environment, Settings
from whatsbot.main import create_app
from whatsbot.ports.media_downloader import DownloadedMedia
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


class FakeDownloader:
    """In-memory downloader that returns a pre-canned JPEG payload."""

    def __init__(self, payload: bytes, mime: str) -> None:
        self.payload = payload
        self.mime = mime
        self.calls: list[str] = []

    def download(self, media_id: str) -> DownloadedMedia:
        self.calls.append(media_id)
        return DownloadedMedia(
            payload=self.payload,
            mime=self.mime,
            sha256=hashlib.sha256(self.payload).hexdigest(),
        )


def _build_text_payload(text: str) -> bytes:
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
                                "phone_number_id": "PID",
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


def _build_image_payload(
    *, media_id: str, mime: str, caption: str | None = None
) -> bytes:
    image_obj: dict[str, str] = {"id": media_id, "mime_type": mime}
    if caption is not None:
        image_obj["caption"] = caption
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
                                "phone_number_id": "PID",
                            },
                            "contacts": [{"wa_id": "491701234567"}],
                            "messages": [
                                {
                                    "from": ALLOWED_SENDER,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": "1745318400",
                                    "type": "image",
                                    "image": image_obj,
                                }
                            ],
                        },
                    }
                ],
            }
        ],
    }
    return json.dumps(payload, separators=(",", ":")).encode()


def _build_pdf_payload(
    *, media_id: str, caption: str | None = None, filename: str = "report.pdf"
) -> bytes:
    doc_obj: dict[str, str] = {
        "id": media_id,
        "mime_type": "application/pdf",
        "filename": filename,
    }
    if caption is not None:
        doc_obj["caption"] = caption
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
                                "phone_number_id": "PID",
                            },
                            "contacts": [{"wa_id": "491701234567"}],
                            "messages": [
                                {
                                    "from": ALLOWED_SENDER,
                                    "id": f"wamid.{uuid.uuid4().hex}",
                                    "timestamp": "1745318400",
                                    "type": "document",
                                    "document": doc_obj,
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
    names: list[str] = []
    yield names
    for name in names:
        subprocess.run(
            ["tmux", "kill-session", "-t", name],
            capture_output=True,
            check=False,
        )


def test_image_happy_path(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project = f"img{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    jpeg_payload = b"\xff\xd8\xff\xe0" + b"\x00" * 256
    downloader = FakeDownloader(payload=jpeg_payload, mime="image/jpeg")

    sender = RecordingSender()
    app = create_app(
        settings=Settings(env=Environment.PROD, media_cache_dir=cache_dir),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        media_downloader=downloader,
    )

    with TestClient(app) as client:
        # Create the project + set it active.
        r = _signed_post(client, _build_text_payload(f"/new {project}"))
        assert r.status_code == 200
        r = _signed_post(client, _build_text_payload(f"/p {project}"))
        assert r.status_code == 200

        # Send the image.
        r = _signed_post(
            client,
            _build_image_payload(
                media_id="MEDIA_123",
                mime="image/jpeg",
                caption="schau dir das an",
            ),
        )
        assert r.status_code == 200

    # Download happened exactly once with our ID.
    assert downloader.calls == ["MEDIA_123"]

    # The payload landed in the cache with the right suffix.
    cached_path = cache_dir / "MEDIA_123.jpg"
    assert cached_path.exists()
    assert cached_path.read_bytes() == jpeg_payload

    # The ack reply reached the sender and mentions the project.
    media_reply = [body for _, body in sender.sent if "📨" in body]
    assert media_reply  # at least one
    assert any(project in body for body in media_reply)


def test_pdf_happy_path(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project = f"pdf{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    pdf_payload = b"%PDF-1.4\n" + b"\x00" * 512
    downloader = FakeDownloader(payload=pdf_payload, mime="application/pdf")

    sender = RecordingSender()
    app = create_app(
        settings=Settings(env=Environment.PROD, media_cache_dir=cache_dir),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        media_downloader=downloader,
    )

    with TestClient(app) as client:
        r = _signed_post(client, _build_text_payload(f"/new {project}"))
        assert r.status_code == 200
        r = _signed_post(client, _build_text_payload(f"/p {project}"))
        assert r.status_code == 200

        r = _signed_post(
            client,
            _build_pdf_payload(
                media_id="DOC_42",
                caption="fasse das zusammen",
            ),
        )
        assert r.status_code == 200

    # Download happened once.
    assert downloader.calls == ["DOC_42"]

    # Payload landed in the cache with ``.pdf`` suffix.
    cached_path = cache_dir / "DOC_42.pdf"
    assert cached_path.exists()
    assert cached_path.read_bytes() == pdf_payload

    # Ack reply mentions PDF + project.
    media_reply = [body for _, body in sender.sent if "📨" in body]
    assert media_reply
    assert any("PDF" in body and project in body for body in media_reply)


def test_pdf_size_over_cap_is_rejected(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    """21 MB payload > 20 MB cap → validation reply, no cache, no send."""
    project = f"big{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    cache_dir = tmp_path / "cache"
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    big_payload = b"%PDF-1.4\n" + b"\x00" * (21 * 1024 * 1024)
    downloader = FakeDownloader(payload=big_payload, mime="application/pdf")

    sender = RecordingSender()
    app = create_app(
        settings=Settings(env=Environment.PROD, media_cache_dir=cache_dir),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        media_downloader=downloader,
    )

    with TestClient(app) as client:
        r = _signed_post(client, _build_text_payload(f"/new {project}"))
        assert r.status_code == 200
        r = _signed_post(client, _build_text_payload(f"/p {project}"))
        assert r.status_code == 200
        r = _signed_post(client, _build_pdf_payload(media_id="BIG"))
        assert r.status_code == 200

    # Cache must be empty — rejection happens before .store().
    assert not (cache_dir / "BIG.pdf").exists()

    # Validation reply surfaces with the size hint.
    bodies = [body for _, body in sender.sent]
    assert any(
        "zu gross" in body.lower() or "zu groß" in body.lower()
        for body in bodies
    )


def test_video_gets_friendly_reject(
    tmp_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    """Unsupported kinds reply with the spec-§9 friendly reject."""
    project = f"vid{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project}")

    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    db_path = tmp_path / "state.db"
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    sender = RecordingSender()
    app = create_app(
        settings=Settings(env=Environment.PROD, media_cache_dir=tmp_path / "c"),
        secrets_provider=_full_secret_stub(),
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary="/bin/true",
        media_downloader=FakeDownloader(b"x", "video/mp4"),
    )

    # Build a video payload.
    video_payload = json.dumps(
        {
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
                                        "type": "video",
                                        "video": {
                                            "id": "V1",
                                            "mime_type": "video/mp4",
                                        },
                                    }
                                ],
                            },
                        }
                    ],
                }
            ],
        },
        separators=(",", ":"),
    ).encode()

    with TestClient(app) as client:
        r = _signed_post(client, video_payload)
        assert r.status_code == 200

    # One friendly reject reply.
    assert len(sender.sent) == 1
    _, body = sender.sent[0]
    assert "Video" in body
