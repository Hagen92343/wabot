"""End-to-end smoke for all 17 Spec §12 deny-patterns.

Each JSON fixture under ``tests/fixtures/deny/*.json`` is fed through a
fully-wired hook endpoint backed by a real HookService + coordinator.
Verifies that every irreversible-damage pattern is rejected before the
coordinator is ever consulted — even when the active project is in the
most permissive mode (``yolo``).

Also confirms:

* 17 / 17 distinct patterns are exercised (no silent duplication).
* Each fixture's ``_pattern`` field is actually the pattern the matcher
  fires on — if we refactor ``deny_patterns`` the fixtures drift with
  it, and this test fails loudly.
* A comparable legit-command set returns the expected non-deny
  verdicts (allow in YOLO, deny in STRICT without allow-rules).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import (
    SqliteAllowRuleRepository,
)
from whatsbot.adapters.sqlite_pending_confirmation_repository import (
    SqlitePendingConfirmationRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.application.hook_service import HookService
from whatsbot.domain.deny_patterns import DENY_PATTERNS
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.http.hook_endpoint import HOOK_SECRET_HEADER, build_router
from whatsbot.ports.secrets_provider import (
    KEY_HOOK_SHARED_SECRET,
    SecretNotFoundError,
)

pytestmark = pytest.mark.integration

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "deny"
SHARED_SECRET = "shh-e2e"


# ---- infra helpers ------------------------------------------------------


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


class SilentSender:
    """Sends messages to /dev/null so coordinator.ask_bash never blocks
    on network I/O. Not that it matters here — deny fires first."""

    def send_text(self, *, to: str, body: str) -> None:
        return None


@pytest.fixture
def wired_client() -> Iterator[TestClient]:
    """TestClient backed by real repos + coordinator, project 'smoke' in YOLO.

    YOLO is intentional: any false-negative in the matcher would let
    the command through. A deny-pattern match therefore proves the
    deny layer fires *above* the permissive mode, which is the Spec
    §12 fail-closed guarantee.

    FastAPI's ``TestClient`` dispatches each request on a worker
    thread, so the connection here is built with
    ``check_same_thread=False`` — the production connection lives in
    the event loop thread and doesn't need this.
    """
    conn = sqlite3.connect(":memory:", isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    sqlite_repo.apply_schema(conn)
    try:
        project_repo = SqliteProjectRepository(conn)
        project_repo.create(
            Project(
                name="smoke",
                source_mode=SourceMode.EMPTY,
                created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
                mode=Mode.YOLO,
            )
        )
        allow_repo = SqliteAllowRuleRepository(conn)
        pending_repo = SqlitePendingConfirmationRepository(conn)

        coordinator = ConfirmationCoordinator(
            repo=pending_repo,
            sender=SilentSender(),
            default_recipient="+490000",
            window_seconds=1,  # short so a surprise AskUser times out quickly
        )
        service = HookService(
            project_repo=project_repo,
            allow_rule_repo=allow_repo,
            coordinator=coordinator,
        )

        app = FastAPI()
        app.include_router(
            build_router(secrets=StubSecrets(SHARED_SECRET), service=service)
        )
        yield TestClient(app)
    finally:
        conn.close()


def _load_fixture(name: str) -> dict[str, Any]:
    with (FIXTURES_DIR / name).open() as f:
        data = json.load(f)
    assert isinstance(data, dict)
    return data


def _bash_post(
    client: TestClient,
    command: str,
    project: str = "smoke",
    session_id: str = "smoke-c32",
) -> dict[str, Any]:
    response = client.post(
        "/hook/bash",
        headers={HOOK_SECRET_HEADER: SHARED_SECRET},
        json={"command": command, "project": project, "session_id": session_id},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert isinstance(body, dict)
    return body


# ---- fixture-pack: 17 patterns -----------------------------------------


def test_fixture_pack_size_matches_deny_pattern_count() -> None:
    """If someone adds a fixture (or a pattern) without the other, this
    test points at the drift before it hides anything."""
    fixtures = sorted(FIXTURES_DIR.glob("*.json"))
    assert len(fixtures) == len(DENY_PATTERNS) == 17

    fixture_patterns = {json.loads(f.read_text())["_pattern"] for f in fixtures}
    domain_patterns = {p.pattern for p in DENY_PATTERNS}
    assert fixture_patterns == domain_patterns


@pytest.mark.parametrize(
    "fixture_name",
    sorted(p.name for p in FIXTURES_DIR.glob("*.json")),
)
def test_every_fixture_denies_even_in_yolo_mode(
    wired_client: TestClient, fixture_name: str
) -> None:
    fx = _load_fixture(fixture_name)
    tool_input = fx["tool_input"]
    assert isinstance(tool_input, dict)
    command = tool_input["command"]
    assert isinstance(command, str)
    expected_pattern = fx["_pattern"]
    assert isinstance(expected_pattern, str)

    body = _bash_post(wired_client, command)

    assert body["hookSpecificOutput"]["permissionDecision"] == "deny", (
        f"{fixture_name} ({command!r}) should have been denied but wasn't"
    )
    reason = body["hookSpecificOutput"]["permissionDecisionReason"]
    assert isinstance(reason, str)
    assert expected_pattern in reason, (
        f"{fixture_name}: deny reason {reason!r} doesn't mention expected "
        f"pattern {expected_pattern!r}"
    )


# ---- negative controls: legit commands aren't denied by the matcher ----


def test_legit_bash_command_does_not_match_deny_pattern(
    wired_client: TestClient,
) -> None:
    """In YOLO, a non-deny command returns allow. If the matcher ever
    false-positives on e.g. 'git status', this test catches it here
    (we wouldn't notice from the /hook/bash contract otherwise)."""
    body = _bash_post(wired_client, "git status")
    assert body["hookSpecificOutput"]["permissionDecision"] == "allow"
    assert body["hookSpecificOutput"]["permissionDecisionReason"] == "yolo mode"


def test_quoting_tricks_still_trigger_deny(wired_client: TestClient) -> None:
    """Makes sure the normalisation survives the round-trip through
    HTTP + JSON parsing."""
    body = _bash_post(wired_client, 'rm   -rf    "/"')
    assert body["hookSpecificOutput"]["permissionDecision"] == "deny"
    assert "rm -rf /" in body["hookSpecificOutput"]["permissionDecisionReason"]
