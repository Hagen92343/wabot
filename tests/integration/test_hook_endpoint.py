"""Integration tests for whatsbot.http.hook_endpoint.

Uses FastAPI's TestClient to exercise the /hook/bash and /hook/write
routes end-to-end: auth, payload validation, decision serialisation.
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whatsbot.application.hook_service import HookService
from whatsbot.http.hook_endpoint import HOOK_SECRET_HEADER, build_router
from whatsbot.ports.secrets_provider import (
    KEY_HOOK_SHARED_SECRET,
    SecretNotFoundError,
)

pytestmark = pytest.mark.integration


class StubSecrets:
    def __init__(self, secret: str | None) -> None:
        self._store: dict[str, str] = {}
        if secret is not None:
            self._store[KEY_HOOK_SHARED_SECRET] = secret

    def get(self, key: str) -> str:
        if key not in self._store:
            raise SecretNotFoundError(key)
        return self._store[key]

    def set(self, key: str, value: str) -> None:
        self._store[key] = value

    def rotate(self, key: str, new_value: str) -> None:
        self._store[key] = new_value


def _app(secret: str | None, service: HookService | None = None) -> TestClient:
    app = FastAPI()
    app.include_router(
        build_router(
            secrets=StubSecrets(secret),
            service=service if service is not None else HookService(),
        )
    )
    return TestClient(app)


# ---- /hook/bash ----------------------------------------------------------


def test_bash_rejects_missing_secret_header() -> None:
    c = _app("shh")
    r = c.post("/hook/bash", json={"command": "ls"})
    assert r.status_code == 401
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_rejects_wrong_secret() -> None:
    c = _app("shh")
    r = c.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: "wrong"},
        json={"command": "ls"},
    )
    assert r.status_code == 401


def test_bash_allows_with_correct_secret() -> None:
    c = _app("shh")
    r = c.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: "shh"},
        json={"command": "ls", "project": "alpha", "session_id": "s1"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
    # In stub mode (no coordinator wired) the service explains why it
    # allowed — useful when sanity-checking the hook round-trip.
    assert "stub" in body["hookSpecificOutput"]["permissionDecisionReason"]


def test_bash_missing_command_is_deny_400() -> None:
    c = _app("shh")
    r = c.post("/hook/bash", headers={HOOK_SECRET_HEADER: "shh"}, json={})
    assert r.status_code == 400
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_empty_command_is_deny_400() -> None:
    c = _app("shh")
    r = c.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: "shh"},
        json={"command": "   "},
    )
    assert r.status_code == 400


def test_bash_malformed_json_is_deny_400() -> None:
    c = _app("shh")
    r = c.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: "shh", "Content-Type": "application/json"},
        content=b"{not json",
    )
    assert r.status_code == 400
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"


def test_bash_missing_server_side_secret_denies_all() -> None:
    """If the Keychain had no hook-shared-secret at build time, every
    request must be denied regardless of header content — otherwise we
    could silently drift into an allow-by-default posture."""
    c = _app(None)
    r = c.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: "anything"},
        json={"command": "ls"},
    )
    assert r.status_code == 401


# ---- /hook/write ---------------------------------------------------------


def test_write_allows_with_correct_secret() -> None:
    c = _app("shh")
    r = c.post(
        "/hook/write",
        headers={HOOK_SECRET_HEADER: "shh"},
        json={"path": "/Users/me/projekte/alpha/README.md", "project": "alpha"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"


def test_write_missing_path_is_deny_400() -> None:
    c = _app("shh")
    r = c.post("/hook/write", headers={HOOK_SECRET_HEADER: "shh"}, json={})
    assert r.status_code == 400


def test_write_rejects_missing_secret_header() -> None:
    c = _app("shh")
    r = c.post("/hook/write", json={"path": "/tmp/x"})
    assert r.status_code == 401


# ---- fail-closed on service crash ---------------------------------------


class CrashingService(HookService):
    """Raises on every classify call so we can verify the endpoint
    catches and returns deny."""

    def classify_bash(self, **kwargs: object) -> object:  # type: ignore[override]
        raise RuntimeError("boom")

    def classify_write(self, **kwargs: object) -> object:  # type: ignore[override]
        raise RuntimeError("boom")


def test_bash_service_crash_returns_deny_200() -> None:
    c = _app("shh", service=CrashingService())
    r = c.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: "shh"},
        json={"command": "ls"},
    )
    # The endpoint catches the service exception and replies deny with 200
    # — returning 500 would make the hook client treat it as unreachable,
    # which is also fail-closed, but "explicit deny" beats "no response"
    # for debuggability.
    assert r.status_code == 200
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "error" in body["hookSpecificOutput"]["permissionDecisionReason"]


def test_write_service_crash_returns_deny_200() -> None:
    c = _app("shh", service=CrashingService())
    r = c.post(
        "/hook/write",
        headers={HOOK_SECRET_HEADER: "shh"},
        json={"path": "/Users/me/projekte/alpha/file"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
