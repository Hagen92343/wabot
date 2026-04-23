"""SessionLockRepository port — Phase 5 persistence for ``session_locks``.

Minimal CRUD: ``get`` / ``upsert`` / ``delete`` / ``list_all``. The
sweeper (``LockService.sweep_expired``) uses ``list_all`` to find
idle-local locks; day-to-day the bot only touches one row at a time
via get/upsert.
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.locks import SessionLock


class SessionLockRepository(Protocol):
    def get(self, project_name: str) -> SessionLock | None:
        """Return the row or ``None`` if no lock was ever written."""

    def upsert(self, lock: SessionLock) -> None:
        """Create or replace the row for ``lock.project_name``."""

    def delete(self, project_name: str) -> bool:
        """Remove the row. ``True`` iff one existed."""

    def list_all(self) -> list[SessionLock]:
        """All locks, ordered by project_name. Sweeper input."""
