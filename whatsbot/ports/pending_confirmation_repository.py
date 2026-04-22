"""PendingConfirmationRepository port — persistence for hook confirmations.

Backed by the ``pending_confirmations`` table from Spec §19. The adapter
lives in ``adapters/sqlite_pending_confirmation_repository.py``; service
code depends only on this Protocol.

Unlike ``PendingDeleteRepository`` (one-row-per-project), confirmations
can be many — a user might have two open Bash prompts at once on
different projects. Each row has a unique ``id`` (ULID upstream).
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.pending_confirmations import PendingConfirmation


class PendingConfirmationRepository(Protocol):
    """CRUD + sweep over ``pending_confirmations``."""

    def create(self, confirmation: PendingConfirmation) -> None:
        """Insert a new confirmation row. IDs are caller-supplied (ULID)
        and must be unique; an insert collision is a programming bug and
        should raise."""

    def get(self, confirmation_id: str) -> PendingConfirmation | None:
        """Return the row, or ``None`` if it's gone (resolved or expired)."""

    def resolve(self, confirmation_id: str) -> bool:
        """Delete the row. Returns ``True`` if a row existed.

        Resolution itself (allow vs deny) is coordinated in memory via the
        service layer — this repo's job is only persistence/audit."""

    def list_open(self) -> list[PendingConfirmation]:
        """All rows, ordered by ``created_at`` (oldest first).

        Used by the WhatsApp command handler to route a PIN answer to the
        right confirmation when multiple are open."""

    def delete_expired(self, now_ts: int) -> list[str]:
        """Sweep rows whose deadline has passed. Returns the confirmation
        IDs that were removed so the caller can log them and signal the
        in-memory listeners waiting on those IDs."""
