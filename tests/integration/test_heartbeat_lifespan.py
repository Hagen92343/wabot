"""Phase 6 C6.4 — heartbeat lifespan integration.

Spins up an in-process FastAPI app via TestClient with the heartbeat
explicitly enabled. The lifespan must:

* write the heartbeat file the moment the app starts up
* keep it fresh while the client is alive
* delete it on shutdown so a restart doesn't see a stale mtime
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.file_heartbeat_writer import FileHeartbeatWriter
from whatsbot.config import Environment, Settings
from whatsbot.main import create_app
from whatsbot.ports.secrets_provider import ALL_KEYS, SecretNotFoundError

pytestmark = pytest.mark.integration


class _StubSecrets:
    def __init__(self) -> None:
        # Pre-populated so PROD env startup doesn't refuse on missing
        # Keychain entries.
        self._store: dict[str, str] = {k: f"placeholder-{k}" for k in ALL_KEYS}

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:  # pragma: no cover
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:  # pragma: no cover
        self._store[key] = new_value


def test_heartbeat_appears_on_startup_and_disappears_on_shutdown(
    tmp_path: Path,
) -> None:
    heartbeat_path = tmp_path / "hb"
    conn = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(conn)

    settings = Settings(
        env=Environment.PROD,
        heartbeat_path=heartbeat_path,
        panic_marker_path=tmp_path / "PANIC",
    )
    app = create_app(
        settings=settings,
        secrets_provider=_StubSecrets(),
        db_connection=conn,
        projects_root=tmp_path / "projekte",
        # Test env explicitly skips real tmux + watchdog adapters,
        # but heartbeat is opt-in so we wire it via the writer.
        heartbeat_writer=FileHeartbeatWriter(heartbeat_path),
        enable_heartbeat=True,
    )

    # Pre-startup: nothing on disk yet.
    assert not heartbeat_path.exists()

    with TestClient(app):
        # Lifespan startup ran → heartbeat is on disk.
        assert heartbeat_path.exists(), (
            "lifespan should have written the heartbeat on startup"
        )
        body = heartbeat_path.read_text(encoding="utf-8")
        assert "whatsbot heartbeat" in body
        assert "pid=" in body

    # Lifespan shutdown ran → heartbeat is removed.
    assert not heartbeat_path.exists(), (
        "lifespan should have removed the heartbeat on shutdown"
    )
