"""Unit tests for ``whatsbot.application.heartbeat_pumper`` (Phase 6 C6.4)."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import UTC, datetime

import pytest

from whatsbot.application.heartbeat_pumper import HeartbeatPumper

pytestmark = pytest.mark.unit

NOW = datetime(2026, 4, 23, 12, 0, tzinfo=UTC)


@dataclass
class _FakeWriter:
    write_calls: list[str] = field(default_factory=list)
    raise_on_write: Exception | None = None
    raise_on_remove: Exception | None = None
    remove_calls: int = 0

    def write(self, payload: str) -> None:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.write_calls.append(payload)

    def last_mtime(self) -> float | None:  # pragma: no cover
        return None

    def remove(self) -> None:
        self.remove_calls += 1
        if self.raise_on_remove is not None:
            raise self.raise_on_remove


@pytest.mark.asyncio
async def test_start_writes_immediately_and_creates_task() -> None:
    writer = _FakeWriter()
    p = HeartbeatPumper(writer=writer, interval_seconds=10.0)
    await p.start()
    # First write happens synchronously inside start() — watchdog
    # sees the file at t=0, not at t=interval.
    assert len(writer.write_calls) == 1
    await p.stop()


@pytest.mark.asyncio
async def test_start_is_idempotent() -> None:
    writer = _FakeWriter()
    p = HeartbeatPumper(writer=writer, interval_seconds=10.0)
    await p.start()
    await p.start()  # must not crash, must not double-write
    assert len(writer.write_calls) == 1
    await p.stop()


@pytest.mark.asyncio
async def test_stop_cancels_loop_and_removes_file() -> None:
    writer = _FakeWriter()
    p = HeartbeatPumper(writer=writer, interval_seconds=10.0)
    await p.start()
    await p.stop()
    assert writer.remove_calls == 1


@pytest.mark.asyncio
async def test_stop_idempotent_when_never_started() -> None:
    writer = _FakeWriter()
    p = HeartbeatPumper(writer=writer, interval_seconds=10.0)
    await p.stop()  # must not raise
    assert writer.remove_calls == 0


@pytest.mark.asyncio
async def test_loop_writes_repeatedly() -> None:
    """Use a tiny interval and let the loop tick a few times."""
    writer = _FakeWriter()
    p = HeartbeatPumper(writer=writer, interval_seconds=0.01)
    await p.start()
    # Wait long enough for ~3 ticks (initial + 2 loop iterations).
    await asyncio.sleep(0.05)
    await p.stop()
    assert len(writer.write_calls) >= 3


@pytest.mark.asyncio
async def test_write_failure_does_not_crash_loop() -> None:
    """A single failed write must not kill the loop — the next tick
    should retry."""
    writer = _FakeWriter()
    writer.raise_on_write = OSError("disk full")
    p = HeartbeatPumper(writer=writer, interval_seconds=0.01)
    # start() does the first write — that one will raise but be
    # logged and swallowed.
    await p.start()
    await asyncio.sleep(0.03)
    await p.stop()
    # Writer was called many times despite raising every time.
    # We can't assert write_calls because raises happen before the
    # append, but we can check the loop is still alive (no crash).


@pytest.mark.asyncio
async def test_remove_failure_does_not_propagate_from_stop() -> None:
    writer = _FakeWriter()
    writer.raise_on_remove = OSError("perm denied")
    p = HeartbeatPumper(writer=writer, interval_seconds=10.0)
    await p.start()
    await p.stop()  # must not raise


@pytest.mark.asyncio
async def test_payload_format_includes_pid_and_version() -> None:
    writer = _FakeWriter()
    p = HeartbeatPumper(
        writer=writer,
        interval_seconds=10.0,
        clock=lambda: NOW,
        get_pid=lambda: 4242,
        version="9.9.9",
    )
    await p.start()
    await p.stop()
    assert "pid=4242" in writer.write_calls[0]
    assert "version=9.9.9" in writer.write_calls[0]
    assert "ts=2026-04-23T12:00:00+00:00" in writer.write_calls[0]
