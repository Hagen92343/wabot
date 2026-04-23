"""HeartbeatPumper — async background loop for the watchdog protocol.

Runs inside the FastAPI lifespan: ``start()`` on app startup,
``stop()`` on app shutdown. Writes the heartbeat file every
``HEARTBEAT_INTERVAL_SECONDS`` (Spec §7).

Design notes:

* The loop catches every exception so a transient write failure
  doesn't kill the pumper. The watchdog treats stale heartbeats
  the same way regardless of *why* they're stale, so trying to
  recover a failed write is pointless — the next tick will retry.
* File IO is offloaded to ``asyncio.to_thread`` so the FastAPI
  event loop never blocks on disk, even though our writes are
  microseconds today. Defensive discipline; trivial cost.
* ``stop()`` is graceful: it cancels the loop, awaits the task,
  and removes the heartbeat file so a restart sees ``None`` as
  the last mtime.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
from collections.abc import Callable
from datetime import UTC, datetime

import whatsbot
from whatsbot.domain.heartbeat import (
    HEARTBEAT_INTERVAL_SECONDS,
    format_heartbeat_payload,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.heartbeat_writer import HeartbeatWriter


class HeartbeatPumper:
    """Background asyncio loop that writes the heartbeat file."""

    def __init__(
        self,
        *,
        writer: HeartbeatWriter,
        interval_seconds: float = float(HEARTBEAT_INTERVAL_SECONDS),
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        get_pid: Callable[[], int] = os.getpid,
        version: str = whatsbot.__version__,
    ) -> None:
        self._writer = writer
        self._interval_s = interval_seconds
        self._clock = clock
        self._get_pid = get_pid
        self._version = version
        self._task: asyncio.Task[None] | None = None
        self._log = get_logger("whatsbot.heartbeat")

    async def start(self) -> None:
        """Begin pumping in the background. Idempotent — a second
        ``start`` call while already running is a no-op (returns the
        existing task implicitly).
        """
        if self._task is not None and not self._task.done():
            return
        # First write happens *before* we go async so the watchdog
        # sees a fresh file immediately, not after the first 30 s.
        await self._write_once()
        self._task = asyncio.create_task(
            self._loop(), name="heartbeat-pumper"
        )
        self._log.info(
            "heartbeat_pumper_started",
            interval_seconds=self._interval_s,
        )

    async def stop(self) -> None:
        """Stop the loop and remove the heartbeat file. Idempotent."""
        task = self._task
        if task is None:
            return
        self._task = None
        task.cancel()
        # The task was cancelled or raised on its way out — both
        # are fine, we don't want shutdown to fail because of it.
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await task
        try:
            await asyncio.to_thread(self._writer.remove)
        except Exception as exc:
            self._log.warning(
                "heartbeat_remove_failed", error=str(exc)
            )
        self._log.info("heartbeat_pumper_stopped")

    async def _loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_s)
                await self._write_once()
        except asyncio.CancelledError:
            # Re-raise so ``stop()``'s ``await`` sees a clean cancel.
            raise

    async def _write_once(self) -> None:
        payload = format_heartbeat_payload(
            now=self._clock(),
            pid=self._get_pid(),
            version=self._version,
        )
        try:
            await asyncio.to_thread(self._writer.write, payload)
        except Exception as exc:
            # Don't blow up the pumper on a single failed write —
            # the watchdog catches stale heartbeats either way.
            self._log.warning(
                "heartbeat_write_failed", error=str(exc)
            )
