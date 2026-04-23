"""Max-limits repository port.

Phase-8 C8.1: persistence of the three Claude usage limits
(session_5h / weekly / opus_sub, Spec §19). The row set is tiny — at
most three rows ever — so we don't optimise reads; every call goes to
the DB. CRUD plus a targeted ``mark_warned`` partial update so the
warning path doesn't have to round-trip the full row.
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.limits import LimitKind, MaxLimit


class MaxLimitsRepository(Protocol):
    def get(self, kind: LimitKind) -> MaxLimit | None: ...

    def upsert(self, limit: MaxLimit) -> None: ...

    def delete(self, kind: LimitKind) -> bool: ...

    def list_all(self) -> list[MaxLimit]: ...

    def mark_warned(self, kind: LimitKind, warned_at_ts: int) -> None:
        """Partial update of just ``warned_at_ts`` — avoids a
        full-row upsert on the warning-side hot path."""
