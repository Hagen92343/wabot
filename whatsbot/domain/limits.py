"""Max-Limit domain model — pure, no I/O.

Phase-8 C8.1. Claude Code surfaces three distinct usage limits on the
Max-20x subscription (Spec §14 + §19):

* ``session_5h`` — rolling 5-hour session budget.
* ``weekly`` — week-long aggregate budget.
* ``opus_sub`` — separate Opus-specific allowance.

Each has its own reset timestamp and its own "remaining %" signal. We
keep them in the ``max_limits`` table keyed by ``kind`` and let the
application layer fire a single WhatsApp warning per window when a
limit drops below :data:`LOW_REMAINING_THRESHOLD`. Everything here is
pure — the service wires in the clock + repository.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Final


class LimitKind(StrEnum):
    """Mirror of the ``max_limits.kind`` check-constraint (Spec §19)."""

    SESSION_5H = "session_5h"
    WEEKLY = "weekly"
    OPUS_SUB = "opus_sub"


LOW_REMAINING_THRESHOLD: Final[float] = 0.10
"""<10% remaining triggers a proactive WhatsApp warning (Spec §14)."""


@dataclass(frozen=True, slots=True)
class MaxLimit:
    """One row of ``max_limits``.

    ``reset_at_ts`` and ``warned_at_ts`` are Unix epoch *seconds*
    (int). That matches the SQLite schema and stays clock-skew-safe
    for the short durations we care about (hours).

    ``remaining_pct`` is a float in ``[0.0, 1.0]`` or the nullable
    ``-1.0`` sentinel when Claude's transcript didn't surface it — the
    warning path treats that as "unknown, don't warn".
    """

    kind: LimitKind
    reset_at_ts: int
    warned_at_ts: int | None = None
    remaining_pct: float = -1.0

    def with_warned(self, ts: int) -> MaxLimit:
        return replace(self, warned_at_ts=ts)


def is_active(limit: MaxLimit, *, now: int) -> bool:
    """A limit is "active" (i.e. should block new prompts) while its
    reset hasn't happened yet."""
    return now < limit.reset_at_ts


def should_warn(limit: MaxLimit, *, now: int) -> bool:
    """Return ``True`` iff we should fire a proactive WhatsApp warning.

    Gate:

    * must be active right now;
    * must have a real ``remaining_pct`` (not the ``-1.0`` sentinel);
    * remaining must be under the 10% threshold;
    * we must not have *already* warned *for this window*. A row's
      ``warned_at_ts`` belongs to the same window as its ``reset_at_ts``
      iff ``warned_at_ts >= (reset_at_ts - 48h)`` — a conservative
      upper-bound for the longest window we track (weekly is 7 days,
      but warnings only fire in the final hours so 48h is plenty of
      "same window" headroom).
    """
    if not is_active(limit, now=now):
        return False
    if limit.remaining_pct < 0:
        return False
    if limit.remaining_pct >= LOW_REMAINING_THRESHOLD:
        return False
    warned = limit.warned_at_ts
    if warned is None:
        return True
    # ``warned_at_ts`` survived a rotation if it's older than the
    # current window. 2-day buffer before the reset is "same window".
    same_window_floor = limit.reset_at_ts - 2 * 24 * 3600
    return warned < same_window_floor


def shortest_active(
    limits: list[MaxLimit], *, now: int
) -> MaxLimit | None:
    """Among the currently-active limits, return the one whose reset
    is *soonest* — Spec §14 "Bei mehreren aktiv: kürzester Countdown
    in Antwort". Returns ``None`` when no limit is active."""
    active = [limit for limit in limits if is_active(limit, now=now)]
    if not active:
        return None
    return min(active, key=lambda limit: limit.reset_at_ts)


def format_reset_duration(reset_at_ts: int, *, now: int) -> str:
    """Pure formatter: ``"3h 22m"`` / ``"42m"`` / ``"15s"``.

    Negative / zero remaining collapses to ``"<1s"`` so the reply
    never says something nonsensical on a freshly-expired row.
    """
    delta = reset_at_ts - now
    if delta <= 0:
        return "<1s"
    hours, remainder = divmod(delta, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes}m"
    if minutes:
        return f"{minutes}m"
    return f"{seconds}s"


def parse_reset_at(raw: object) -> int | None:
    """Best-effort parser for the ``reset_at`` value on a
    :class:`whatsbot.domain.transcript.UsageLimitEvent`.

    Accepts:

    * ``None`` → ``None``.
    * ``int`` / ``float`` → direct epoch seconds (truncated).
    * ``str`` → ISO-8601 (with or without fractional seconds, with or
      without ``Z`` / timezone offset). Missing TZ is treated as UTC.

    Returns an int epoch-seconds or ``None`` on any parse failure so
    callers can fall back to a default window.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):  # bool is a subclass of int — reject explicitly
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if not isinstance(raw, str):
        return None
    candidate = raw.strip()
    if not candidate:
        return None
    # Normalise the ``Z`` suffix — ``datetime.fromisoformat`` got
    # ``Z`` support in 3.11+. Double-handle for robustness.
    if candidate.endswith("Z") or candidate.endswith("z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return int(parsed.timestamp())
