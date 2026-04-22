"""ClaudeSessionRepository port — persistence for Claude session state.

Backed by the ``claude_sessions`` table from Spec §19. One row per
project. The adapter lives in
``adapters/sqlite_claude_session_repository.py``.

Minimal operation set for Phase 4:

* ``upsert`` — create or replace the row on ``ensure_started`` /
  ``recycle``.
* ``get`` — lookup by project name; None if no session.
* ``list_all`` — reboot recovery iterates this.
* ``delete`` — called from ``/rm`` flow when a project goes away
  (the ``ON DELETE CASCADE`` in the schema already covers it, but
  explicit deletes are useful in tests and bot-side bookkeeping).

Field-level updates (tokens, activity, mode) are fast paths on
top of the shared upsert. They keep the hot-path turn-ingest loop
from rewriting unrelated columns on every assistant event.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import ClaudeSession


class ClaudeSessionRepository(Protocol):
    """CRUD over ``claude_sessions``."""

    def upsert(self, session: ClaudeSession) -> None:
        """Create or replace the row for ``session.project_name``."""

    def get(self, project_name: str) -> ClaudeSession | None:
        """Return the row or ``None`` if the project has no session yet."""

    def list_all(self) -> list[ClaudeSession]:
        """All sessions, ordered by ``project_name``. Used by the
        startup-recovery sweeper and the upcoming ``/ps`` command."""

    def delete(self, project_name: str) -> bool:
        """Remove the row. Returns ``True`` iff one existed."""

    # ---- Hot-path partial updates ------------------------------------
    #
    # These touch only the columns named and leave the rest alone. The
    # service layer uses them for the transcript-ingest loop where a
    # full ``upsert`` would over-write ``current_mode`` that might have
    # been switched concurrently.

    def update_activity(
        self, project_name: str, *, tokens_used: int, last_activity_at: datetime
    ) -> None:
        """Bump ``tokens_used`` / ``context_fill_ratio`` / ``last_activity_at``
        in one statement. ``turns_count`` is left to a separate path so
        partial-turn token updates don't miscount turns."""

    def bump_turn(self, project_name: str, *, at: datetime) -> None:
        """Increment ``turns_count`` and refresh ``last_activity_at``."""

    def update_mode(self, project_name: str, mode: Mode) -> None:
        """Switch ``current_mode`` only. Called by ``/mode`` after the
        projects-table update has committed."""

    def mark_compact(self, project_name: str, at: datetime) -> None:
        """Persist the ``last_compact_at`` + reset ``tokens_used`` /
        ``context_fill_ratio`` to 0 (Spec §8: post-compact we start fresh)."""
