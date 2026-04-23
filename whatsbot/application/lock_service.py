"""LockService — Phase-5 soft-preemption use-cases.

Wraps ``domain.locks`` + a ``SessionLockRepository`` in the three
operations the bot needs on the hot path:

* ``acquire_for_bot(project)`` — called from
  ``SessionService.send_prompt`` before handing a prompt to tmux.
  Raises ``LocalTerminalHoldsLockError`` if the local terminal is
  actively holding the session; the command handler turns that
  into the Spec-§7 ``🔒 Terminal aktiv`` reply.
* ``note_local_input(project)`` — called from
  ``TranscriptIngest._handle_user`` when a non-ZWSP user-turn
  arrives, i.e. a human typed at the tmux pane. Flips owner to
  LOCAL (pre-empting any prior bot lock).
* ``release(project)`` — ``/release`` command.
* ``force_bot(project)`` — ``/force`` override (PIN-gated in
  the command layer).
* ``sweep_expired()`` — reaper for idle LOCAL locks. Cheap — the
  Phase-5 startup path can call it on every bot restart without
  racing the live acquires.

Clock is injected so tests don't have to sleep.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from whatsbot.domain.locks import (
    LOCK_TIMEOUT_SECONDS,
    AcquireOutcome,
    LockOwner,
    SessionLock,
    evaluate_bot_attempt,
    is_expired,
    mark_local_input,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.session_lock_repository import SessionLockRepository


class LocalTerminalHoldsLockError(RuntimeError):
    """Raised by ``acquire_for_bot`` when the local terminal is
    actively holding the lock for this project. The command handler
    catches it and turns it into ``🔒 Terminal aktiv ...``."""

    def __init__(self, project_name: str) -> None:
        self.project_name = project_name
        super().__init__(
            f"Local terminal holds the lock for project {project_name!r}"
        )


@dataclass(frozen=True, slots=True)
class AcquireResult:
    """What ``acquire_for_bot`` returns on success. Distinguishes a
    fresh grant from an auto-release-then-grant so logs / audits can
    record the pre-emption."""

    outcome: AcquireOutcome
    lock: SessionLock


class LockService:
    def __init__(
        self,
        *,
        repo: SessionLockRepository,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        timeout_seconds: int = LOCK_TIMEOUT_SECONDS,
        on_owner_change: Callable[[str], None] | None = None,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._timeout_s = timeout_seconds
        self._on_owner_change = on_owner_change
        self._log = get_logger("whatsbot.locks")

    # ---- bot-side operations -----------------------------------------

    def acquire_for_bot(self, project_name: str) -> AcquireResult:
        """Try to take the lock for the bot. Raises on DENIED_LOCAL_HELD."""
        now = self._clock()
        current = self._repo.get(project_name)
        outcome, new_state = evaluate_bot_attempt(
            current,
            now=now,
            timeout_seconds=self._timeout_s,
            project_name=project_name,
        )
        if outcome is AcquireOutcome.DENIED_LOCAL_HELD:
            self._log.info(
                "lock_acquire_denied",
                project=project_name,
                owner=new_state.owner.value,
            )
            raise LocalTerminalHoldsLockError(project_name)

        self._repo.upsert(new_state)
        self._log.info(
            "lock_acquired_for_bot",
            project=project_name,
            outcome=outcome.value,
        )
        if current is None or current.owner is not new_state.owner:
            self._notify_owner_change(project_name)
        return AcquireResult(outcome=outcome, lock=new_state)

    def force_bot(self, project_name: str) -> SessionLock:
        """Unconditional bot acquire — used by ``/force`` (PIN-gated)."""
        now = self._clock()
        previous = self._repo.get(project_name)
        new_state = SessionLock(
            project_name=project_name,
            owner=LockOwner.BOT,
            acquired_at=now,
            last_activity_at=now,
        )
        self._repo.upsert(new_state)
        self._log.warning(
            "lock_forced_for_bot", project=project_name
        )
        if previous is None or previous.owner is not LockOwner.BOT:
            self._notify_owner_change(project_name)
        return new_state

    # ---- observer-side operations ------------------------------------

    def note_local_input(self, project_name: str) -> SessionLock:
        """Mark that the local terminal is in play. Called from
        the transcript ingest thread."""
        now = self._clock()
        current = self._repo.get(project_name)
        new_state = mark_local_input(
            current, now=now, project_name=project_name
        )
        self._repo.upsert(new_state)
        if current is None or current.owner is not LockOwner.LOCAL:
            self._log.info(
                "lock_taken_by_local",
                project=project_name,
                preempted_from=(
                    current.owner.value if current is not None else "none"
                ),
            )
            self._notify_owner_change(project_name)
        return new_state

    # ---- release / sweep --------------------------------------------

    def release(self, project_name: str) -> bool:
        """Drop the lock back to ``free``. Returns whether a row existed
        to delete — False is fine (nothing to release).
        """
        removed = self._repo.delete(project_name)
        if removed:
            self._log.info("lock_released", project=project_name)
            self._notify_owner_change(project_name)
        return removed

    def sweep_expired(self) -> list[str]:
        """Delete every lock that's idle-local past the timeout. Returns
        the list of projects touched (for audit / test assertions)."""
        now = self._clock()
        reaped: list[str] = []
        for lock in self._repo.list_all():
            if is_expired(lock, now=now, timeout_seconds=self._timeout_s):
                self._repo.delete(lock.project_name)
                reaped.append(lock.project_name)
        if reaped:
            self._log.info("lock_sweep_reaped", projects=reaped)
            for name in reaped:
                self._notify_owner_change(name)
        return reaped

    # ---- internals ---------------------------------------------------

    def _notify_owner_change(self, project_name: str) -> None:
        """Fire the on_owner_change callback, swallowing exceptions.

        The callback drives a tmux status-bar repaint — purely
        cosmetic, so a failure here must never break the underlying
        lock operation.
        """
        if self._on_owner_change is None:
            return
        try:
            self._on_owner_change(project_name)
        except Exception as exc:
            self._log.warning(
                "lock_owner_change_callback_failed",
                project=project_name,
                error=str(exc),
            )

    # ---- read-only ---------------------------------------------------

    def current(self, project_name: str) -> SessionLock | None:
        """Expose the current lock row for status-bar rendering."""
        return self._repo.get(project_name)
