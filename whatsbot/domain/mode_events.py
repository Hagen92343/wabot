"""Mode-event audit records — pure domain model.

Every permission-mode transition (user-initiated switch, reboot-
driven YOLO reset, ``/panic`` reset, session recycle after Claude
crash) is recorded in the ``mode_events`` table (Spec §19). The
rows are an audit trail — read by ``/log``, ``/errors``, and
post-incident forensics.

This module is pure; the sqlite adapter consumes ``ModeEvent``
dataclasses and writes them. IDs are minted by the application
layer (ULID) so the table never depends on a DB-side sequence.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from whatsbot.domain.projects import Mode


class ModeEventKind(StrEnum):
    """Matches the ``CHECK(event IN (...))`` constraint in the schema.

    Phase 4 introduces ``switch`` (user-initiated ``/mode``). The
    other three are wired in later phases: ``reboot_reset`` fires
    from the startup-recovery path (C4.7), ``panic_reset`` from
    ``/panic`` (Phase 6), ``session_recycle`` from the watchdog's
    tmux-dead detection (Phase 6 again).
    """

    SWITCH = "switch"
    REBOOT_RESET = "reboot_reset"
    PANIC_RESET = "panic_reset"
    SESSION_RECYCLE = "session_recycle"


@dataclass(frozen=True, slots=True)
class ModeEvent:
    """One row of ``mode_events``.

    ``from_mode`` is optional because reboot-reset events can land
    before we know the old mode (the DB row may already have been
    coerced). ``msg_id`` threads the ULID correlation-id from the
    inbound WhatsApp message so the audit trail links back to the
    request that triggered the switch.
    """

    id: str
    project_name: str
    kind: ModeEventKind
    to_mode: Mode
    at: datetime
    from_mode: Mode | None = None
    msg_id: str | None = None
