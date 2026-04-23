"""ModeEventRepository port — audit append-only for ``mode_events``.

Spec §19 defines the schema; this port keeps the surface narrow:
only ``record`` for writes + ``list_for_project`` for the upcoming
Phase-8 ``/log`` inspection path. No updates or deletes — the
audit log is grow-only by design.
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.mode_events import ModeEvent


class ModeEventRepository(Protocol):
    def record(self, event: ModeEvent) -> None:
        """Persist a mode transition. Idempotent on the primary key —
        attempting to re-record the same ``id`` raises."""

    def list_for_project(self, project_name: str) -> list[ModeEvent]:
        """All events for a project, newest first. Used by ``/log``
        diagnostics in Phase 8; tests in Phase 4 read this to
        verify a switch was written."""
