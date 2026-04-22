"""Unit tests for ConfirmationCoordinator — async PIN round-trip."""

from __future__ import annotations

import asyncio

import pytest

from whatsbot.application.confirmation_coordinator import (
    ConfirmationCoordinator,
    ResolveResult,
)
from whatsbot.domain.hook_decisions import Verdict
from whatsbot.domain.pending_confirmations import PendingConfirmation

pytestmark = pytest.mark.unit


# --- Stub infra --------------------------------------------------------


class _FakeRepo:
    """In-memory stand-in for PendingConfirmationRepository."""

    def __init__(self, *, fail_on_create: bool = False) -> None:
        self.rows: dict[str, PendingConfirmation] = {}
        self.fail_on_create = fail_on_create
        self.resolves: list[str] = []

    def create(self, confirmation: PendingConfirmation) -> None:
        if self.fail_on_create:
            raise RuntimeError("simulated DB crash")
        self.rows[confirmation.id] = confirmation

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        return self.rows.get(confirmation_id)

    def resolve(self, confirmation_id: str) -> bool:
        self.resolves.append(confirmation_id)
        return self.rows.pop(confirmation_id, None) is not None

    def list_open(self) -> list[PendingConfirmation]:
        return sorted(self.rows.values(), key=lambda c: c.created_at)

    def delete_expired(self, now_ts: int) -> list[str]:
        expired = [cid for cid, c in self.rows.items() if c.deadline_ts <= now_ts]
        for cid in expired:
            del self.rows[cid]
        return expired


class _FakeSender:
    def __init__(self, *, fail: bool = False) -> None:
        self.messages: list[tuple[str, str]] = []
        self.fail = fail

    def send_text(self, *, to: str, body: str) -> None:
        if self.fail:
            raise RuntimeError("simulated sender failure")
        self.messages.append((to, body))


# --- Happy path --------------------------------------------------------


def test_ask_bash_returns_allow_when_pin_accepted() -> None:
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo,
        sender=sender,
        default_recipient="+4917600000000",
        window_seconds=5,
    )

    async def scenario() -> None:
        # Start the ask, then race-resolve with the PIN.
        task = asyncio.create_task(
            coord.ask_bash(command="rm -rf /var/log/*", project="alpha", reason="demo")
        )
        # Let ask_bash register the future before we resolve.
        await asyncio.sleep(0)
        result = coord.try_resolve("1234", pin="1234")
        assert isinstance(result, ResolveResult)
        assert result.accepted is True

        decision = await task
        assert decision.verdict is Verdict.ALLOW
        assert "confirmed" in decision.reason.lower()
        assert coord.open_count == 0

    asyncio.run(scenario())
    # The persisted row should be cleaned up after resolution.
    assert repo.rows == {}
    # User got exactly one WhatsApp prompt.
    assert len(sender.messages) == 1
    assert sender.messages[0][0] == "+4917600000000"
    assert "rm -rf /var/log/*" in sender.messages[0][1]


def test_ask_bash_returns_deny_when_user_says_no() -> None:
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo,
        sender=sender,
        default_recipient="+4917600000000",
        window_seconds=5,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            coord.ask_bash(command="dangerous", project="alpha", reason="why")
        )
        await asyncio.sleep(0)
        result = coord.try_resolve("nein", pin="1234")
        assert result is not None and result.accepted is False
        decision = await task
        assert decision.verdict is Verdict.DENY
        assert "reject" in decision.reason.lower()

    asyncio.run(scenario())


def test_ask_bash_reject_case_insensitive() -> None:
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo,
        sender=sender,
        default_recipient="+49",
        window_seconds=5,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            coord.ask_bash(command="x", project="p", reason="r")
        )
        await asyncio.sleep(0)
        coord.try_resolve("NEIN", pin="1234")
        decision = await task
        assert decision.verdict is Verdict.DENY

    asyncio.run(scenario())


# --- Timeout -----------------------------------------------------------


def test_ask_bash_times_out_when_no_answer() -> None:
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo,
        sender=sender,
        default_recipient="+49",
        window_seconds=0,  # instant timeout for the test
    )

    async def scenario() -> None:
        decision = await coord.ask_bash(command="x", project="p", reason="r")
        assert decision.verdict is Verdict.DENY
        assert "timed out" in decision.reason.lower()
        assert coord.open_count == 0

    asyncio.run(scenario())
    assert repo.rows == {}  # cleaned up


# --- Failure modes -----------------------------------------------------


def test_ask_bash_denies_when_repo_persist_fails() -> None:
    repo = _FakeRepo(fail_on_create=True)
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo,
        sender=sender,
        default_recipient="+49",
        window_seconds=5,
    )

    async def scenario() -> None:
        decision = await coord.ask_bash(command="x", project="p", reason="r")
        assert decision.verdict is Verdict.DENY
        assert "failed to open" in decision.reason.lower()
        assert coord.open_count == 0
        # No WhatsApp attempted when persistence already failed.
        assert sender.messages == []

    asyncio.run(scenario())


def test_ask_bash_still_waits_when_sender_fails() -> None:
    """A failed WhatsApp send doesn't sabotage the round-trip — user can
    still type the PIN if they notice the hook blocked something."""
    repo = _FakeRepo()
    sender = _FakeSender(fail=True)
    coord = ConfirmationCoordinator(
        repo=repo,
        sender=sender,
        default_recipient="+49",
        window_seconds=5,
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            coord.ask_bash(command="x", project="p", reason="r")
        )
        await asyncio.sleep(0)
        coord.try_resolve("1234", pin="1234")
        decision = await task
        assert decision.verdict is Verdict.ALLOW

    asyncio.run(scenario())


# --- try_resolve edge cases -------------------------------------------


def test_try_resolve_returns_none_with_no_pending() -> None:
    repo = _FakeRepo()
    coord = ConfirmationCoordinator(
        repo=repo, sender=_FakeSender(), default_recipient="+49"
    )
    assert coord.try_resolve("1234", pin="1234") is None
    assert coord.try_resolve("nein", pin="1234") is None


def test_try_resolve_ignores_non_pin_non_reject_text() -> None:
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo, sender=sender, default_recipient="+49", window_seconds=5
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            coord.ask_bash(command="x", project="p", reason="r")
        )
        await asyncio.sleep(0)
        # Random text that isn't the PIN and isn't "nein" is passed through.
        assert coord.try_resolve("hello", pin="1234") is None
        assert coord.try_resolve("", pin="1234") is None
        assert coord.try_resolve("   ", pin="1234") is None
        assert coord.open_count == 1
        # Real answer resolves.
        coord.try_resolve("1234", pin="1234")
        decision = await task
        assert decision.verdict is Verdict.ALLOW

    asyncio.run(scenario())


def test_try_resolve_fifo_order_with_multiple_open() -> None:
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo, sender=sender, default_recipient="+49", window_seconds=5
    )

    async def scenario() -> None:
        t1 = asyncio.create_task(
            coord.ask_bash(command="first", project="p", reason="r")
        )
        await asyncio.sleep(0)
        await asyncio.sleep(0.01)  # ensure distinct created_at
        t2 = asyncio.create_task(
            coord.ask_bash(command="second", project="p", reason="r")
        )
        await asyncio.sleep(0)

        # First PIN resolves the older one.
        r1 = coord.try_resolve("1234", pin="1234")
        assert r1 is not None
        d1 = await t1
        assert d1.verdict is Verdict.ALLOW

        # Second PIN resolves the remaining one.
        r2 = coord.try_resolve("nein", pin="1234")
        assert r2 is not None and r2.accepted is False
        d2 = await t2
        assert d2.verdict is Verdict.DENY

    asyncio.run(scenario())


def test_try_resolve_empty_pin_never_matches() -> None:
    """A missing PIN secret must not accidentally authorise every answer."""
    repo = _FakeRepo()
    sender = _FakeSender()
    coord = ConfirmationCoordinator(
        repo=repo, sender=sender, default_recipient="+49", window_seconds=1
    )

    async def scenario() -> None:
        task = asyncio.create_task(
            coord.ask_bash(command="x", project="p", reason="r")
        )
        await asyncio.sleep(0)
        # Empty answer, empty pin — must not accidentally succeed.
        assert coord.try_resolve("", pin="") is None
        # Random guess with empty pin — must not match either.
        assert coord.try_resolve("1234", pin="") is None
        # Still pending.
        assert coord.open_count == 1
        # Let it time out.
        decision = await task
        assert decision.verdict is Verdict.DENY

    asyncio.run(scenario())


def test_open_count_is_zero_initially() -> None:
    coord = ConfirmationCoordinator(
        repo=_FakeRepo(), sender=_FakeSender(), default_recipient="+49"
    )
    assert coord.open_count == 0
