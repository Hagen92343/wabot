"""PendingOutputRepository port — persistence for oversized-output dialogues.

Backed by the ``pending_outputs`` table from Spec §19. The adapter lives
in ``adapters/sqlite_pending_output_repository.py``; service code depends
only on this Protocol.
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.pending_outputs import PendingOutput


class PendingOutputRepository(Protocol):
    """CRUD + sweep over ``pending_outputs``."""

    def create(self, output: PendingOutput) -> None:
        """Insert a new row. IDs are caller-supplied (ULID) and unique;
        a collision is a programming bug and must raise."""

    def get(self, msg_id: str) -> PendingOutput | None:
        """Return the row, or ``None`` if it's gone (resolved or expired)."""

    def latest_open(self) -> PendingOutput | None:
        """Most recently created row (``created_at`` DESC).

        Single-user assumption: when the user types ``/send``/``/discard``/
        ``/save`` we always act on the one they just saw a warning for.
        Stale rows are swept separately by ``delete_expired``."""

    def resolve(self, msg_id: str) -> bool:
        """Delete the row. Returns ``True`` if a row existed."""

    def delete_expired(self, now_ts: int) -> list[str]:
        """Sweep rows whose deadline has passed. Returns the msg-ids that
        were removed so the caller can unlink the matching files."""
