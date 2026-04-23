"""End-to-end prompt-roundtrip smoke — the full C4.2 goal.

Covers the whole pipe: ``/p <name> <prompt>`` via ``/webhook``
→ ``SessionService.send_prompt`` → tmux → headless-claude stub
→ stub writes assistant event to the transcript file
→ ``WatchdogTranscriptWatcher`` picks up the line
→ ``TranscriptIngest`` fires ``on_turn_end``
→ ``OutputService.deliver`` hands the text to the sender.

Skipped when ``tmux`` isn't installed (same gating as the other
integration tests). Each test uses a unique project name + its
own tmp ``claude_home`` + a wrapper script that sets
``HEADLESS_CLAUDE_HOME`` so concurrent runs can't collide.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import shutil
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.adapters.watchdog_transcript_watcher import (
    WatchdogTranscriptWatcher,
)
from whatsbot.application.session_service import SessionService
from whatsbot.config import Environment, Settings
from whatsbot.main import create_app
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

HEADLESS_STUB = (
    Path(__file__).resolve().parents[1] / "fixtures" / "headless_claude.py"
)


# ---- stubs ---------------------------------------------------------


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


def _build_meta_payload(text: str) -> bytes:
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
                                "phone_number_id": "PHONE_NUMBER_ID",
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


def _make_stub_wrapper(
    tmp_path: Path,
    *,
    claude_home: Path,
    reply: str,
) -> Path:
    """Shell wrapper that sets HEADLESS_CLAUDE_* env vars and execs
    the python stub. Needed because tmux inherits env from its server
    at startup — we can't fake env vars for an already-running tmux
    server by just modifying ``os.environ`` in the test process."""
    wrapper = tmp_path / "stub-safe-claude.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'export HEADLESS_CLAUDE_HOME={claude_home}\n'
        f'export HEADLESS_CLAUDE_REPLY="{reply}"\n'
        f'exec {sys.executable} {HEADLESS_STUB} "$@"\n'
    )
    wrapper.chmod(0o755)
    return wrapper


@pytest.fixture
def projects_root(tmp_path: Path) -> Path:
    root = tmp_path / "projekte"
    root.mkdir()
    return root


@pytest.fixture
def claude_home(tmp_path: Path) -> Path:
    root = tmp_path / "claude-home"
    root.mkdir()
    return root


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


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


def _wait_for_reply_containing(
    sender: RecordingSender,
    needle: str,
    *,
    timeout_seconds: float = 8.0,
) -> None:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if any(needle in body for _, body in sender.sent):
            return
        time.sleep(0.1)


def test_prompt_roundtrip_delivers_assistant_reply_to_whatsapp(
    tmp_path: Path,
    projects_root: Path,
    claude_home: Path,
    db_path: Path,
    tmux_session_cleanup: list[str],
) -> None:
    project_name = f"round{uuid.uuid4().hex[:4]}"
    tmux_session_cleanup.append(f"wb-{project_name}")
    reply_marker = f"MARKER-{uuid.uuid4().hex[:8]}"
    wrapper = _make_stub_wrapper(
        tmp_path, claude_home=claude_home, reply=reply_marker
    )

    sender = RecordingSender()
    secrets = _full_secret_stub()
    # On-disk DB with check_same_thread=False (set by sqlite_repo.connect)
    # so the TestClient worker thread + ingest observer thread + watcher
    # thread can all share the same connection.
    conn = sqlite_repo.connect(str(db_path))
    sqlite_repo.apply_schema(conn)

    watcher = WatchdogTranscriptWatcher()
    app = create_app(
        settings=Settings(env=Environment.PROD),
        secrets_provider=secrets,
        message_sender=sender,
        db_connection=conn,
        projects_root=projects_root,
        tmux_controller=SubprocessTmuxController(),
        safe_claude_binary=str(wrapper),
        transcript_watcher=watcher,
        claude_home=claude_home,
        discovery_timeout_seconds=6.0,
    )

    try:
        with TestClient(app) as client:
            # /new alpha — create the project.
            resp = _signed_post(
                client, _build_meta_payload(f"/new {project_name}")
            )
            assert resp.status_code == 200
            # /p alpha <prompt> — triggers send_prompt → stub → transcript.
            resp = _signed_post(
                client, _build_meta_payload(f"/p {project_name} hi Claude")
            )
            assert resp.status_code == 200

            _wait_for_reply_containing(sender, reply_marker)

            # Assistant text reached the sender as a separate
            # message alongside the /p ack.
            bodies = [body for _, body in sender.sent]
            assert any(
                reply_marker in body for body in bodies
            ), f"marker {reply_marker!r} not in {bodies!r}"
    finally:
        # Tear down the watcher's observer threads explicitly so
        # the next test starts clean.
        session_service = app.state.session_service
        if isinstance(session_service, SessionService):
            for project in list(session_service._watches.keys()):
                session_service.stop_transcript_watch(project)
