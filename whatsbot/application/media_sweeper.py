"""MediaSweeper — background loop that enforces Spec §16 cache policy.

Fires every ``DEFAULT_SWEEP_INTERVAL_SECONDS`` (10 minutes) and runs
two passes against :meth:`MediaCache.list_all`:

1. **TTL sweep**: every item older than 7 days gets secure-deleted.
2. **Size sweep**: if the remaining cache is still over 1 GB,
   oldest-first items are secure-deleted until we're under cap.

Design mirrors :class:`whatsbot.application.heartbeat_pumper.HeartbeatPumper`:

* asyncio.Task with idempotent ``start()`` / ``stop()`` semantics.
* File IO goes through ``asyncio.to_thread`` so the FastAPI event
  loop never blocks on stat / unlink.
* Every delete failure is log-only — a stuck file on disk must not
  kill the sweeper. The next tick retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass

from whatsbot.domain.media_cache import (
    CACHE_MAX_BYTES,
    CACHE_TTL_SECONDS,
    select_expired,
    select_for_eviction,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.media_cache import MediaCache

DEFAULT_SWEEP_INTERVAL_SECONDS: float = 10 * 60  # 10 minutes


@dataclass(frozen=True, slots=True)
class SweepReport:
    """Counters from a single sweep — handy for tests and the future
    ``/status``-style diagnostic."""

    ttl_deleted: int
    size_deleted: int
    bytes_freed: int


class MediaSweeper:
    """Background asyncio loop that enforces media-cache retention."""

    def __init__(
        self,
        *,
        cache: MediaCache,
        interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
        ttl_seconds: int = CACHE_TTL_SECONDS,
        max_bytes: int = CACHE_MAX_BYTES,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache = cache
        self._interval_s = interval_seconds
        self._ttl_s = ttl_seconds
        self._max_bytes = max_bytes
        self._clock = clock
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("whatsbot.media_sweeper")

    async def start(self) -> None:
        """Begin sweeping in the background. Idempotent."""
        if self._task is not None and not self._task.done():
            return
        # Run one sweep immediately so a freshly-started bot doesn't
        # carry a previous run's overflow into the next 10 minutes.
        await self._sweep_once()
        self._task = asyncio.create_task(
            self._loop(), name="media-sweeper"
        )
        self._log.info(
            "media_sweeper_started",
            interval_seconds=self._interval_s,
            ttl_seconds=self._ttl_s,
            max_bytes=self._max_bytes,
        )

    async def stop(self) -> None:
        """Stop the loop. Idempotent. No file IO on shutdown — we
        don't want to block shutdown on a slow secure-delete pass."""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._log.info("media_sweeper_stopped")

    async def sweep_now(self) -> SweepReport:
        """Run one sweep on demand. Used by tests and by an optional
        future ``/status media-sweep`` command."""
        return await self._sweep_once()

    # ---- internals ---------------------------------------------------

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                await self._sweep_once()
        except asyncio.CancelledError:
            raise

    async def _sweep_once(self) -> SweepReport:
        try:
            items = await asyncio.to_thread(self._cache.list_all)
        except Exception as exc:
            self._log.warning("media_sweep_list_failed", error=str(exc))
            return SweepReport(ttl_deleted=0, size_deleted=0, bytes_freed=0)

        now = self._clock()
        expired = select_expired(items, now=now, ttl_seconds=self._ttl_s)

        ttl_deleted = 0
        bytes_freed = 0
        for item in expired:
            if await self._delete(item.path):
                ttl_deleted += 1
                bytes_freed += item.size_bytes

        # Re-list after TTL-sweep so the size calculation reflects the
        # post-delete state. A second list is cheap (stat + iterdir).
        if ttl_deleted:
            try:
                items = await asyncio.to_thread(self._cache.list_all)
            except Exception as exc:
                self._log.warning(
                    "media_sweep_list_failed_after_ttl", error=str(exc)
                )
                items = []

        victims = select_for_eviction(items, max_size=self._max_bytes)
        size_deleted = 0
        for item in victims:
            if await self._delete(item.path):
                size_deleted += 1
                bytes_freed += item.size_bytes

        if ttl_deleted or size_deleted:
            self._log.info(
                "media_sweep_complete",
                ttl_deleted=ttl_deleted,
                size_deleted=size_deleted,
                bytes_freed=bytes_freed,
            )
        return SweepReport(
            ttl_deleted=ttl_deleted,
            size_deleted=size_deleted,
            bytes_freed=bytes_freed,
        )

    async def _delete(self, path: "object") -> bool:
        try:
            # MediaCache.secure_delete takes a Path — mypy already knows.
            await asyncio.to_thread(self._cache.secure_delete, path)  # type: ignore[arg-type]
            return True
        except Exception as exc:
            self._log.warning(
                "media_sweep_delete_failed",
                path=str(path),
                error=str(exc),
            )
            return False
