"""PendingOutput domain model — pure, no I/O.

A ``PendingOutput`` is the bookkeeping for an oversized-body dialogue:
the bot stashed a long body on disk, sent the user a size warning, and
is waiting for them to type ``/send``, ``/discard``, or ``/save``.

Schema: ``pending_outputs`` (Spec §19).

The default retention window is 24 hours — longer than the
Hook-confirmation 5-minute window because the user may legitimately
want to think before approving a bulk send, and the content is on disk
rather than holding an open Future.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

PENDING_OUTPUT_WINDOW_SECONDS: Final[int] = 24 * 3600  # 24h


@dataclass(frozen=True, slots=True)
class PendingOutput:
    """One row of ``pending_outputs``."""

    msg_id: str
    project_name: str
    output_path: str
    size_bytes: int
    created_at: datetime
    deadline_ts: int

    def is_expired(self, now_ts: int) -> bool:
        return now_ts >= self.deadline_ts


def compute_deadline(now_ts: int, window: int = PENDING_OUTPUT_WINDOW_SECONDS) -> int:
    """Deadline in epoch-seconds, ``window`` seconds from now."""
    if window < 0:
        raise ValueError(f"window must be >= 0, got {window}")
    return now_ts + window
