"""C7.5 — MediaSweeper async-loop tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from whatsbot.application.media_sweeper import (
    DEFAULT_SWEEP_INTERVAL_SECONDS,
    MediaSweeper,
    SweepReport,
)
from whatsbot.domain.media_cache import CACHE_TTL_SECONDS
from whatsbot.ports.media_cache import CachedItem


pytestmark = pytest.mark.asyncio


class FakeCache:
    """In-memory MediaCache that records calls and can be seeded with
    a specific list_all result."""

    def __init__(
        self,
        *,
        items: list[CachedItem] | None = None,
        list_error: Exception | None = None,
        delete_errors: dict[Path, Exception] | None = None,
    ) -> None:
        self._items = list(items or [])
        self._list_error = list_error
        self._delete_errors = delete_errors or {}
        self.list_calls = 0
        self.deleted: list[Path] = []

    def list_all(self) -> list[CachedItem]:
        self.list_calls += 1
        if self._list_error is not None:
            raise self._list_error
        return list(self._items)

    def secure_delete(self, path: Path) -> None:
        err = self._delete_errors.get(path)
        if err is not None:
            raise err
        self.deleted.append(path)
        self._items = [item for item in self._items if item.path != path]

    # Unused in sweeper tests but required by MediaCache protocol.
    def store(self, media_id: str, payload: bytes, suffix: str) -> Path:  # pragma: no cover
        raise NotImplementedError

    def path_for(self, media_id: str, suffix: str) -> Path:  # pragma: no cover
        raise NotImplementedError


def _item(name: str, *, size: int, mtime: float) -> CachedItem:
    return CachedItem(path=Path(f"/tmp/c/{name}"), size_bytes=size, mtime=mtime)


# ---- sweep_now: pure one-shot runs --------------------------------------


async def test_sweep_removes_ttl_expired_items() -> None:
    now = 1_000_000.0
    items = [
        _item("fresh", size=10, mtime=now - 60),
        _item("stale", size=20, mtime=now - CACHE_TTL_SECONDS - 1),
    ]
    cache = FakeCache(items=items)
    sweeper = MediaSweeper(cache=cache, clock=lambda: now)
    report = await sweeper.sweep_now()
    assert isinstance(report, SweepReport)
    assert report.ttl_deleted == 1
    assert report.size_deleted == 0
    assert report.bytes_freed == 20
    assert [p.name for p in cache.deleted] == ["stale"]


async def test_sweep_evicts_over_cap_oldest_first() -> None:
    now = 1_000_000.0
    # All fresh — TTL shouldn't fire. 3 × 500 bytes over a 1000 cap.
    items = [
        _item("a", size=500, mtime=now - 3),
        _item("b", size=500, mtime=now - 2),
        _item("c", size=500, mtime=now - 1),
    ]
    cache = FakeCache(items=items)
    sweeper = MediaSweeper(
        cache=cache, max_bytes=1000, clock=lambda: now
    )
    report = await sweeper.sweep_now()
    assert report.ttl_deleted == 0
    assert report.size_deleted == 1
    assert report.bytes_freed == 500
    assert [p.name for p in cache.deleted] == ["a"]  # oldest evicted


async def test_sweep_combines_ttl_and_size() -> None:
    now = 1_000_000.0
    items = [
        # Expired — should go first.
        _item("stale", size=100, mtime=now - CACHE_TTL_SECONDS - 1),
        # Fresh but together over cap.
        _item("fresh_oldest", size=500, mtime=now - 3),
        _item("fresh_mid", size=500, mtime=now - 2),
        _item("fresh_newest", size=500, mtime=now - 1),
    ]
    cache = FakeCache(items=items)
    sweeper = MediaSweeper(
        cache=cache, max_bytes=1000, clock=lambda: now
    )
    report = await sweeper.sweep_now()
    assert report.ttl_deleted == 1
    assert report.size_deleted == 1
    assert report.bytes_freed == 100 + 500
    # Order: stale first (TTL pass), then fresh_oldest (size pass).
    assert [p.name for p in cache.deleted] == ["stale", "fresh_oldest"]


async def test_sweep_under_cap_no_op() -> None:
    now = 1_000_000.0
    items = [_item("a", size=10, mtime=now - 60)]
    cache = FakeCache(items=items)
    sweeper = MediaSweeper(
        cache=cache, max_bytes=1000, clock=lambda: now
    )
    report = await sweeper.sweep_now()
    assert report == SweepReport(
        ttl_deleted=0, size_deleted=0, bytes_freed=0
    )
    assert cache.deleted == []


# ---- failure containment -------------------------------------------------


async def test_sweep_list_failure_returns_zero_report() -> None:
    cache = FakeCache(list_error=OSError("disk died"))
    sweeper = MediaSweeper(cache=cache)
    report = await sweeper.sweep_now()
    assert report.ttl_deleted == 0
    assert report.size_deleted == 0
    assert report.bytes_freed == 0


async def test_sweep_delete_failure_is_contained() -> None:
    """A failing secure_delete must not halt the sweep — other victims
    still get a chance."""
    now = 1_000_000.0
    items = [
        _item("stuck", size=10, mtime=now - CACHE_TTL_SECONDS - 1),
        _item("ok", size=20, mtime=now - CACHE_TTL_SECONDS - 2),
    ]
    cache = FakeCache(
        items=items,
        delete_errors={items[0].path: OSError("EBUSY")},
    )
    sweeper = MediaSweeper(cache=cache, clock=lambda: now)
    report = await sweeper.sweep_now()
    # "stuck" failed to delete — counted as 0; "ok" succeeded.
    assert report.ttl_deleted == 1
    assert report.bytes_freed == 20
    assert [p.name for p in cache.deleted] == ["ok"]


# ---- start/stop lifecycle ------------------------------------------------


async def test_start_runs_initial_sweep_immediately() -> None:
    now = 1_000_000.0
    items = [_item("stale", size=10, mtime=now - CACHE_TTL_SECONDS - 1)]
    cache = FakeCache(items=items)
    sweeper = MediaSweeper(
        cache=cache,
        interval_seconds=1.0,  # unused — we stop before the first tick
        clock=lambda: now,
    )
    await sweeper.start()
    try:
        # The initial sweep ran synchronously inside start().
        assert [p.name for p in cache.deleted] == ["stale"]
    finally:
        await sweeper.stop()


async def test_start_is_idempotent() -> None:
    cache = FakeCache(items=[])
    sweeper = MediaSweeper(cache=cache, interval_seconds=1.0)
    await sweeper.start()
    initial_calls = cache.list_calls
    await sweeper.start()  # second call should not re-run an immediate sweep
    assert cache.list_calls == initial_calls
    await sweeper.stop()


async def test_stop_is_idempotent() -> None:
    cache = FakeCache(items=[])
    sweeper = MediaSweeper(cache=cache, interval_seconds=1.0)
    await sweeper.start()
    await sweeper.stop()
    await sweeper.stop()  # must not raise


async def test_stop_without_start_is_noop() -> None:
    sweeper = MediaSweeper(cache=FakeCache(items=[]))
    await sweeper.stop()  # must not raise


# ---- sanity: default interval is 10 minutes ------------------------------


async def test_default_sweep_interval_is_10_minutes() -> None:
    # Async to keep pytest-asyncio happy with this file's module-level mark.
    # The assertion itself is sync but an async test with a sync body is fine.
    assert DEFAULT_SWEEP_INTERVAL_SECONDS == 600


# ---- loop actually runs periodic sweeps when time passes ----------------


async def test_loop_runs_periodic_sweep() -> None:
    """Fast-path: 50 ms interval + one seeded stale item = at least
    one sweep should have fired within ~150 ms."""
    now = [1_000_000.0]
    items = [
        _item("stale1", size=10, mtime=now[0] - CACHE_TTL_SECONDS - 1),
    ]
    cache = FakeCache(items=items)
    sweeper = MediaSweeper(
        cache=cache,
        interval_seconds=0.05,
        clock=lambda: now[0],
    )
    await sweeper.start()
    try:
        await asyncio.sleep(0.15)
        # Initial sweep + at least one periodic tick = ≥ 2 list_all calls.
        assert cache.list_calls >= 2
    finally:
        await sweeper.stop()
