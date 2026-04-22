"""PendingDeleteRepository port — persistence for the ``/rm`` confirm flow.

Backed by the ``pending_deletes`` table from Spec §19. The adapter lives
in ``adapters/sqlite_pending_delete_repository.py``; service code depends
only on this Protocol.
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.pending_deletes import PendingDelete


class PendingDeleteRepository(Protocol):
    """CRUD over ``pending_deletes``. One row per project at most — a second
    ``/rm <name>`` before confirmation simply updates the deadline."""

    def upsert(self, pending: PendingDelete) -> None:
        """Insert or update the pending row for ``pending.project_name``."""

    def get(self, project_name: str) -> PendingDelete | None:
        """Return the pending row, or ``None`` if there isn't one."""

    def delete(self, project_name: str) -> bool:
        """Remove the pending row. Returns ``True`` if a row was deleted."""

    def delete_expired(self, now_ts: int) -> list[str]:
        """Sweep rows whose deadline has passed. Returns the project names
        that were removed so the caller can log them."""
