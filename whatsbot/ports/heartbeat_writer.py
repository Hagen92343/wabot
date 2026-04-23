"""Heartbeat-writer port — abstract over the touch-file IO.

Phase 6 wraps a tiny file writer behind a port so the
``HeartbeatPumper`` application service can be unit-tested with an
in-memory fake (no /tmp pollution, no flake on slow CI).
"""

from __future__ import annotations

from typing import Protocol


class HeartbeatWriter(Protocol):
    """Atomic touch-file writer for the watchdog protocol."""

    def write(self, payload: str) -> None:
        """Persist ``payload`` to the heartbeat path. Implementations
        must be **atomic** (write to a sibling tmp file then
        ``os.replace``) so a watchdog reading mid-write never sees
        a half-written file."""

    def last_mtime(self) -> float | None:
        """Return the mtime of the heartbeat file, or ``None`` if it
        doesn't exist yet. Used by the bot's startup self-check
        (a fresh heartbeat at startup means another instance is
        running)."""

    def remove(self) -> None:
        """Best-effort delete the heartbeat file on graceful
        shutdown so a bot restart doesn't see its own leftover
        timestamp as a 'still alive' signal."""
