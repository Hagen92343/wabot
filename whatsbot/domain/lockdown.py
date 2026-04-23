"""Lockdown domain — Spec §7 emergency-state representation.

Pure, no I/O. The application layer (``LockdownService``) is responsible
for persistence (``app_state.lockdown`` row + the touch-file marker).

A lockdown is *engaged* when the bot has decided it should stop
processing normal commands until the user explicitly clears it. Two
ways in:

* ``/panic`` — user-initiated. Reason ``"panic"``.
* The Phase-6 watchdog (later) — when the heartbeat goes stale and the
  watchdog has to step in. Reason ``"watchdog"``.

One way out: ``/unlock <PIN>`` (lands in C6.6).

When engaged, the CommandHandler will block every non-allow-listed
command. ``StartupRecovery`` reads the same state and refuses to
auto-restart sessions during the next bot launch.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

LOCKDOWN_REASON_PANIC: Final = "panic"
LOCKDOWN_REASON_WATCHDOG: Final = "watchdog"
LOCKDOWN_REASON_MANUAL: Final = "manual"

ALL_LOCKDOWN_REASONS: Final = frozenset(
    {
        LOCKDOWN_REASON_PANIC,
        LOCKDOWN_REASON_WATCHDOG,
        LOCKDOWN_REASON_MANUAL,
    }
)


@dataclass(frozen=True, slots=True)
class LockdownState:
    """One snapshot of the lockdown flag.

    ``engaged_at`` and ``reason`` only carry a value when ``engaged``
    is True. The frozen dataclass keeps the shape consistent so the
    serializer doesn't have to special-case ``None`` fields beyond
    the boolean check.
    """

    engaged: bool
    engaged_at: datetime | None = None
    reason: str | None = None
    engaged_by: str | None = None


def disengaged() -> LockdownState:
    """Return the canonical "not engaged" state.

    Helper so callers don't have to remember the constructor shape.
    """
    return LockdownState(engaged=False)


def engage(
    current: LockdownState,
    *,
    now: datetime,
    reason: str,
    engaged_by: str | None = None,
) -> LockdownState:
    """Return the engaged-state to persist.

    Idempotent: if ``current`` is already engaged, the original
    ``engaged_at`` / ``reason`` / ``engaged_by`` are preserved so
    forensics can see *the first* trigger, not the latest re-engage.

    ``reason`` must be one of the documented constants — anything
    else raises ``ValueError`` so a typo doesn't ship a meaningless
    string into the audit log.
    """
    if reason not in ALL_LOCKDOWN_REASONS:
        raise ValueError(
            f"Unknown lockdown reason {reason!r}. "
            f"Use one of: {sorted(ALL_LOCKDOWN_REASONS)}."
        )
    if current.engaged:
        return current
    return LockdownState(
        engaged=True,
        engaged_at=now,
        reason=reason,
        engaged_by=engaged_by,
    )
