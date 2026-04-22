"""Pending-delete domain model — pure, no I/O.

A ``PendingDelete`` is the bookkeeping for the 60-second confirmation
window between ``/rm <name>`` and ``/rm <name> <PIN>`` (Spec §11 +
phase-2.md C2.7). The row is stored in ``pending_deletes`` and cleaned up
either by a successful confirm, an explicit cleanup sweep, or the next
``request_delete`` call on the same project (which resets the deadline).

Design choices:

* Deadlines are epoch-seconds integers. That matches the ``pending_deletes``
  schema (``deadline_ts INTEGER``) and avoids timezone drift — the caller
  compares against ``time.time()`` or an injected clock.
* The confirm window is a module constant (``CONFIRM_WINDOW_SECONDS = 60``)
  so tests and commands share the same number.
* Everything here is *pure* logic. Persistence lives behind
  ``PendingDeleteRepository``; filesystem/PIN work lives in
  ``application.delete_service``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

CONFIRM_WINDOW_SECONDS: Final[int] = 60


@dataclass(frozen=True, slots=True)
class PendingDelete:
    """Pending ``/rm`` request for a single project."""

    project_name: str
    deadline_ts: int

    def is_expired(self, now_ts: int) -> bool:
        return now_ts >= self.deadline_ts

    def seconds_left(self, now_ts: int) -> int:
        """Non-negative seconds until the window closes. 0 after expiry."""
        return max(0, self.deadline_ts - now_ts)


def compute_deadline(now_ts: int, window: int = CONFIRM_WINDOW_SECONDS) -> int:
    """Deadline in epoch-seconds, ``window`` seconds from now.

    Exposed as a function (not a ``PendingDelete`` classmethod) so the
    service layer can stay testable with a fixed ``now_ts``.
    """
    if window < 0:
        raise ValueError(f"window must be >= 0, got {window}")
    return now_ts + window
