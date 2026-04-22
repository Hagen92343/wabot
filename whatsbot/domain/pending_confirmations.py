"""Pending-confirmation domain model — pure, no I/O.

A ``PendingConfirmation`` tracks "I asked the user something on WhatsApp
and I'm waiting for a PIN answer" — a generic version of the 60-second
/rm window, but for the Pre-Tool-Hook (Spec §7): a Bash command matched
a deny pattern or fell through in Normal mode, so the hook is parked
until the user types the PIN on their phone.

Design choices:

* The confirm window is **5 minutes** (Spec §7, §11). That's longer
  than /rm's 60s because the user may be mid-call, mid-coffee, or
  still reading the WhatsApp prompt.
* Deadlines are epoch-seconds integers — same convention as
  ``pending_deletes`` so the cleanup sweepers stay uniform.
* ``kind`` is a bounded enum. New kinds (write-hook confirmations, mode
  switches, whatever) get added here, not ad-hoc in random callers.
* The ``payload`` field is free-form JSON-serialised text — the service
  layer decides the schema per kind. The repository treats it as opaque.
* Everything here is pure; persistence lives behind
  ``PendingConfirmationRepository``, in-memory coordination
  (``asyncio.Future``) lives in the service layer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Final

CONFIRM_WINDOW_SECONDS: Final[int] = 5 * 60  # Spec §7: max 5 min wait


class ConfirmationKind(StrEnum):
    """Bounded set of confirmation kinds. Add new ones here.

    Values match the ``pending_confirmations.kind`` CHECK-free column
    (Spec §19) — we enforce the set at the Python layer instead of the
    DB to keep schema migrations out of the normal feature loop.
    """

    HOOK_BASH = "hook_bash"
    HOOK_WRITE = "hook_write"


@dataclass(frozen=True, slots=True)
class PendingConfirmation:
    """One row of ``pending_confirmations`` (Spec §19)."""

    id: str
    kind: ConfirmationKind
    payload: str  # JSON-serialised; opaque to this module
    deadline_ts: int
    created_at: datetime
    project_name: str | None = None
    msg_id: str | None = None

    def is_expired(self, now_ts: int) -> bool:
        return now_ts >= self.deadline_ts

    def seconds_left(self, now_ts: int) -> int:
        """Non-negative seconds until the window closes."""
        return max(0, self.deadline_ts - now_ts)


def compute_deadline(now_ts: int, window: int = CONFIRM_WINDOW_SECONDS) -> int:
    """Deadline in epoch-seconds, ``window`` seconds from now."""
    if window < 0:
        raise ValueError(f"window must be >= 0, got {window}")
    return now_ts + window
