"""Input-lock domain — soft-preemption between the bot and the local terminal.

Spec §7. One Claude session can have two masters: the WhatsApp bot
(sending prompts via ``tmux send-keys``) and the user typing
directly at the tmux pane. If both race on a shared session at the
same moment, we lose characters or interleave tokens. The lock
pair-serialises them.

Policy (Spec §7):

* Local terminal **wins by default** — the human at the Mac is the
  authoritative user, the WhatsApp bot is a convenience.
* If ``/p <name> <prompt>`` hits while owner is ``local``, the
  prompt is REJECTED. The user is offered ``/force <name> <prompt>``
  to override (PIN-gated).
* If the lock has been sitting on ``local`` for more than
  ``LOCK_TIMEOUT_SECONDS`` without activity, we auto-release it
  back to ``free`` and treat the bot attempt as a fresh acquire.
* Local input pre-empts the bot: when the transcript-watcher sees
  a non-bot-prefixed user event, we flip owner to ``local`` even
  if the bot was mid-turn.

This module is pure — ``datetime``-typed timestamps in / out, no
clocks or I/O inside.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import Final

# Default lock timeout. Spec §7 documents this as 60s — long enough
# for a human-terminal pause, short enough that a closed laptop
# lid doesn't leave a stale lock for hours.
LOCK_TIMEOUT_SECONDS: Final[int] = 60


class LockOwner(StrEnum):
    """Matches the ``CHECK(owner IN (...))`` constraint in the
    ``session_locks`` schema (Spec §19)."""

    FREE = "free"
    BOT = "bot"
    LOCAL = "local"


class AcquireOutcome(StrEnum):
    """Three shapes ``evaluate_bot_attempt`` can return.

    * ``GRANTED`` — lock is now the bot's.
    * ``AUTO_RELEASED_THEN_GRANTED`` — local had it but was idle past
      the timeout; we silently released and re-acquired for the bot.
    * ``DENIED_LOCAL_HELD`` — local still holds it; bot must retry or
      the user must ``/force``.
    """

    GRANTED = "granted"
    AUTO_RELEASED_THEN_GRANTED = "auto_released_then_granted"
    DENIED_LOCAL_HELD = "denied_local_held"


@dataclass(frozen=True, slots=True)
class SessionLock:
    """One row of ``session_locks``.

    ``acquired_at`` is when the current owner first took the lock;
    ``last_activity_at`` is refreshed each time that owner re-engages
    (new bot prompt, new local keystroke detected). The latter is the
    input to the timeout check.
    """

    project_name: str
    owner: LockOwner
    acquired_at: datetime
    last_activity_at: datetime


def evaluate_bot_attempt(
    current: SessionLock | None,
    *,
    now: datetime,
    timeout_seconds: int = LOCK_TIMEOUT_SECONDS,
    project_name: str,
) -> tuple[AcquireOutcome, SessionLock]:
    """Decide what happens when the bot tries to take the lock.

    ``current`` is the existing row (``None`` on first acquire).
    Returns the outcome plus the *post-transition* lock record
    the caller should persist.
    """
    if current is None or current.owner is LockOwner.FREE:
        return AcquireOutcome.GRANTED, SessionLock(
            project_name=project_name,
            owner=LockOwner.BOT,
            acquired_at=now,
            last_activity_at=now,
        )

    if current.owner is LockOwner.BOT:
        # Bot re-acquires — just refresh activity.
        return AcquireOutcome.GRANTED, replace(current, last_activity_at=now)

    # current.owner is LocalLock.LOCAL.
    idle = (now - current.last_activity_at).total_seconds()
    if idle >= timeout_seconds:
        return AcquireOutcome.AUTO_RELEASED_THEN_GRANTED, SessionLock(
            project_name=project_name,
            owner=LockOwner.BOT,
            acquired_at=now,
            last_activity_at=now,
        )
    return AcquireOutcome.DENIED_LOCAL_HELD, current


def mark_local_input(
    current: SessionLock | None,
    *,
    now: datetime,
    project_name: str,
) -> SessionLock:
    """Return the lock record after observing local terminal input.

    If the bot held the lock, this pre-empts it — the user at the
    terminal wins. If the lock was free or local already, we just
    refresh ``last_activity_at``.
    """
    if current is None or current.owner is not LockOwner.LOCAL:
        return SessionLock(
            project_name=project_name,
            owner=LockOwner.LOCAL,
            acquired_at=now,
            last_activity_at=now,
        )
    return replace(current, last_activity_at=now)


def is_expired(
    lock: SessionLock | None,
    *,
    now: datetime,
    timeout_seconds: int = LOCK_TIMEOUT_SECONDS,
) -> bool:
    """True iff ``lock`` is held by ``local`` *and* idle past the timeout.

    The sweeper uses this to reap stale locks; ``evaluate_bot_attempt``
    uses the same rule inline so callers don't have to run a sweep
    before every acquire.
    """
    if lock is None or lock.owner is not LockOwner.LOCAL:
        return False
    return (now - lock.last_activity_at).total_seconds() >= timeout_seconds


# Owner badges shown in the tmux status bar (Spec §6 layout). ``None``
# is treated as FREE — the bot only stores a row once a lock has been
# claimed, so a missing row means nothing-holds-it.
_OWNER_BADGE: Final[dict[LockOwner, str]] = {
    LockOwner.BOT: "🤖 BOT",
    LockOwner.LOCAL: "👤 LOCAL",
    LockOwner.FREE: "— FREE",
}


def lock_owner_badge(owner: LockOwner | None) -> str:
    """Short emoji badge for the current lock owner.

    Pure lookup — adapters consume the string directly when painting
    the tmux status bar. ``None`` (no row in ``session_locks``) renders
    as the FREE badge so the bar always tells the user *something*.
    """
    if owner is None:
        return _OWNER_BADGE[LockOwner.FREE]
    return _OWNER_BADGE[owner]
