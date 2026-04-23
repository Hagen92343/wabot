"""MaxLimitSweeper — periodic tick that fires proactive warnings
and prunes expired rows.

Phase-8 C8.1. Mirrors the :class:`MediaSweeper` pattern: asyncio.Task
started / stopped in the FastAPI lifespan, idempotent start, disk/DB
work offloaded via ``asyncio.to_thread`` so the event loop doesn't
stall on a slow SQLite operation.

Default cadence is 60 s — slow enough that a 7-day-window weekly
limit doesn't warn twice in one minute, fast enough that a
5-hour-window session limit reaches the 10 % threshold warning
within a few minutes of the event landing.
"""

from __future__ import annotations

import asyncio
import contextlib

from whatsbot.application.limit_service import LimitService
from whatsbot.logging_setup import get_logger

DEFAULT_SWEEP_INTERVAL_SECONDS: float = 60.0


class MaxLimitSweeper:
    def __init__(
        self,
        *,
        limit_service: LimitService,
        interval_seconds: float = DEFAULT_SWEEP_INTERVAL_SECONDS,
    ) -> None:
        self._limits = limit_service
        self._interval_s = interval_seconds
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("whatsbot.limit_sweeper")

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        # One immediate tick so a freshly-started bot emits warnings
        # for already-low limits it might have carried over from a
        # previous run.
        await self._tick()
        self._task = asyncio.create_task(
            self._loop(), name="max-limit-sweeper"
        )
        self._log.info(
            "max_limit_sweeper_started",
            interval_seconds=self._interval_s,
        )

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        self._log.info("max_limit_sweeper_stopped")

    async def tick_now(self) -> None:
        """Run one sweep immediately (tests + /status)."""
        await self._tick()

    # ---- internals --------------------------------------------------

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                await self._tick()
        except asyncio.CancelledError:
            raise

    async def _tick(self) -> None:
        try:
            await asyncio.to_thread(self._limits.maybe_warn)
        except Exception as exc:
            self._log.warning(
                "max_limit_warn_failed", error=str(exc)
            )
        try:
            await asyncio.to_thread(self._limits.sweep_expired)
        except Exception as exc:
            self._log.warning(
                "max_limit_sweep_failed", error=str(exc)
            )
