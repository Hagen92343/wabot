"""Phase-4 C4.4 + C4.5 regression: ``/mode`` changes the behaviour
of the Pre-Tool-Hook without restarting the hook service.

Shares one SQLite DB between ``ModeService`` (writes the mode) and
``HookService`` (reads the mode on every ``classify_bash``). If the
hook ever caches the mode or skips the project lookup, this test
catches it.

Coverage:

* Unknown command in ``normal`` → ``AskUser`` (coordinator is called,
  the human PIN/nein round-trip handles it).
* Unknown command in ``strict`` → silent ``deny`` (no coordinator
  call — Spec §12 Layer 2 "allow-list only" invariant).
* Unknown command in ``yolo`` → ``allow`` (``--dangerously-skip-
  permissions`` mode: permissive by default).
* Deny-pattern command stays ``deny`` in all three modes — Spec §12
  Layer-2 deny-blacklist fires above the mode.
"""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from whatsbot.adapters import sqlite_repo
from whatsbot.adapters.sqlite_allow_rule_repository import (
    SqliteAllowRuleRepository,
)
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_pending_confirmation_repository import (
    SqlitePendingConfirmationRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.application.hook_service import HookService
from whatsbot.application.mode_service import ModeService
from whatsbot.application.session_service import SessionService
from whatsbot.domain.hook_decisions import HookDecision, Verdict
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.domain.sessions import ClaudeSession

pytestmark = pytest.mark.integration


class _SilentSender:
    """Swallows outbound WhatsApp messages — we don't care whether
    the ask-the-human prompt lands; we only care whether the
    coordinator was *asked* in the first place."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    def send_text(self, *, to: str, body: str) -> None:
        self.sent.append((to, body))


class _NoopTmux:
    """Stub TmuxController. ModeService.change_mode calls
    session_service.recycle which in turn touches tmux — we don't
    need a real session, so every method is a no-op."""

    def __init__(self) -> None:
        self._alive: set[str] = set()

    def has_session(self, name: str) -> bool:
        return name in self._alive

    def new_session(self, name: str, *, cwd: object) -> None:
        del cwd
        self._alive.add(name)

    def send_text(self, name: str, text: str) -> None:
        del name, text

    def kill_session(self, name: str) -> bool:
        existed = name in self._alive
        self._alive.discard(name)
        return existed

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        del prefix
        return sorted(self._alive)

    def set_status(self, name: str, *, color: str, label: str) -> None:
        del name, color, label


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    c = sqlite_repo.connect(":memory:")
    sqlite_repo.apply_schema(c)
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def services(
    conn: sqlite3.Connection,
    tmp_path: Path,
) -> tuple[HookService, ModeService, _SilentSender]:
    project_repo = SqliteProjectRepository(conn)
    project_repo.create(
        Project(
            name="alpha",
            source_mode=SourceMode.EMPTY,
            created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            mode=Mode.NORMAL,
        )
    )
    SqliteClaudeSessionRepository(conn).upsert(
        ClaudeSession(
            project_name="alpha",
            session_id="sess-alpha",
            transcript_path="",
            started_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
            current_mode=Mode.NORMAL,
        )
    )

    sender = _SilentSender()
    # Short window so the AskUser path times out quickly — we don't
    # test the confirmation round-trip here, just that it *was*
    # attempted (via coordinator state).
    coordinator = ConfirmationCoordinator(
        repo=SqlitePendingConfirmationRepository(conn),
        sender=sender,
        default_recipient="+491701234567",
        window_seconds=1,
    )
    hook_service = HookService(
        project_repo=project_repo,
        allow_rule_repo=SqliteAllowRuleRepository(conn),
        coordinator=coordinator,
    )
    projects_root = tmp_path / "projekte"
    projects_root.mkdir()
    session_service = SessionService(
        project_repo=project_repo,
        session_repo=SqliteClaudeSessionRepository(conn),
        tmux=_NoopTmux(),
        projects_root=projects_root,
    )
    mode_service = ModeService(
        project_repo=project_repo,
        session_repo=SqliteClaudeSessionRepository(conn),
        mode_event_repo=SqliteModeEventRepository(conn),
        session_service=session_service,
    )
    return hook_service, mode_service, sender


def _classify(hook_service: HookService, command: str) -> Verdict:
    # classify_bash is async — run it on a fresh loop so the test
    # function stays sync.
    decision = asyncio.run(_run_classify(hook_service, command))
    return decision.verdict


async def _run_classify(
    hook_service: HookService, command: str
) -> HookDecision:
    return await hook_service.classify_bash(
        command=command,
        project="alpha",
        session_id="s-alpha",
    )


# ---- unknown command follows the active mode -----------------------


def test_unknown_command_changes_verdict_across_mode_switches(
    services: tuple[HookService, ModeService, _SilentSender],
) -> None:
    hook_service, mode_service, sender = services

    # Normal → AskUser. Coordinator times out (window_seconds=1) and
    # returns Deny by default, but what we really care about is that
    # the coordinator *sent an ask-the-human* prompt, which indicates
    # AskUser branch was entered.
    t_start = time.monotonic()
    normal_verdict = _classify(hook_service, "make deploy-prod")
    normal_elapsed = time.monotonic() - t_start
    assert sender.sent, "Normal mode should have asked the human"
    # Coordinator timeout puts the elapsed around window_seconds=1s.
    assert normal_elapsed >= 0.9, "AskUser should actually wait"
    assert normal_verdict is Verdict.DENY  # coordinator times out → deny

    # Strict → silent deny. No ask should happen.
    sender.sent.clear()
    mode_service.change_mode("alpha", Mode.STRICT)
    t_start = time.monotonic()
    strict_verdict = _classify(hook_service, "make deploy-prod")
    strict_elapsed = time.monotonic() - t_start
    assert strict_verdict is Verdict.DENY
    assert sender.sent == [], "Strict must NOT ask the human"
    # And it shouldn't have blocked on the coordinator window.
    assert strict_elapsed < 0.5

    # YOLO → allow unknown.
    mode_service.change_mode("alpha", Mode.YOLO)
    yolo_verdict = _classify(hook_service, "make deploy-prod")
    assert yolo_verdict is Verdict.ALLOW


# ---- deny-patterns survive every mode ----------------------------------


@pytest.mark.parametrize(
    "target_mode",
    [Mode.NORMAL, Mode.STRICT, Mode.YOLO],
)
def test_deny_pattern_blocks_in_every_mode(
    services: tuple[HookService, ModeService, _SilentSender],
    target_mode: Mode,
) -> None:
    """Spec §12 Layer-2 invariant: a ``deny`` rule fires before the
    mode gate. ``git push --force`` is in the Spec-§12 pattern list
    and is the canonical regression fixture for all three modes."""
    hook_service, mode_service, sender = services
    if target_mode is not Mode.NORMAL:
        mode_service.change_mode("alpha", target_mode)

    sender.sent.clear()
    verdict = _classify(hook_service, "git push --force origin main")
    assert verdict is Verdict.DENY
    # A deny-pattern match never consults the coordinator; that's the
    # whole point of the Spec §12 Layer-2 invariant.
    assert sender.sent == []
