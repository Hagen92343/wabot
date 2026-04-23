"""KillService — Phase 6 emergency-control use-cases.

Wraps the two non-panic kill paths from Spec §7 / §11:

* ``stop(project_name)`` — soft cancel. Sends Ctrl+C into the project's
  tmux pane via ``TmuxController.interrupt``. Claude aborts its current
  turn but the session, the DB row and the lock all stay intact, so a
  follow-up ``/p`` can resume immediately.
* ``kill(project_name)`` — hard kill. Destroys the tmux session
  (Claude exits as a side-effect when the pane goes), and releases
  the lock so the user isn't left with a stale ``BOT`` lock pointing
  at a dead Claude. The ``claude_sessions`` row stays — that's how
  the Spec §6/§8 ``--resume`` story works on the next ``/p``.

``/panic`` is intentionally **not** here — it lives in its own service
in C6.2 because the choreography (lockdown, cross-project sweep,
``pkill`` backstop, mode reset) is a different shape than these two
per-project ops.

Both methods accept missing-tmux gracefully — better to ack ``not alive``
than raise on a race where the user sends ``/stop`` after ``/kill``
landed a microsecond earlier.
"""

from __future__ import annotations

from dataclasses import dataclass

from whatsbot.application.lock_service import LockService
from whatsbot.domain.projects import validate_project_name
from whatsbot.domain.sessions import tmux_session_name
from whatsbot.logging_setup import get_logger
from whatsbot.ports.tmux_controller import TmuxController, TmuxError


@dataclass(frozen=True, slots=True)
class StopOutcome:
    """Result of ``KillService.stop``."""

    project_name: str
    was_alive: bool


@dataclass(frozen=True, slots=True)
class KillOutcome:
    """Result of ``KillService.kill``."""

    project_name: str
    was_alive: bool
    lock_released: bool


class KillService:
    """High-level operations backing ``/stop`` and ``/kill``."""

    def __init__(
        self,
        *,
        tmux: TmuxController,
        lock_service: LockService | None = None,
    ) -> None:
        self._tmux = tmux
        self._locks = lock_service
        self._log = get_logger("whatsbot.kill")

    # ---- /stop -----------------------------------------------------

    def stop(self, raw_name: str) -> StopOutcome:
        """Send Ctrl+C to the project's tmux pane.

        No-op (returns ``was_alive=False``) if the session has already
        gone — the user might race ``/stop`` against a recent
        ``/kill`` or against a Claude crash.
        """
        name = validate_project_name(raw_name)
        tmux_name = tmux_session_name(name)
        if not self._tmux.has_session(tmux_name):
            self._log.info("kill_stop_no_session", project=name)
            return StopOutcome(project_name=name, was_alive=False)
        self._tmux.interrupt(tmux_name)
        self._log.info("kill_stop_interrupted", project=name)
        return StopOutcome(project_name=name, was_alive=True)

    # ---- /kill -----------------------------------------------------

    def kill(self, raw_name: str) -> KillOutcome:
        """Destroy the tmux session and release the project's lock.

        ``claude_sessions`` row is intentionally preserved — Phase 6's
        kill is a stop-the-bleeding op, not a project delete. The next
        ``/p <name>`` will see the row and use ``--resume`` to
        continue the conversation. Use ``/rm <name> <PIN>`` if the
        user actually wants to nuke the project.
        """
        name = validate_project_name(raw_name)
        tmux_name = tmux_session_name(name)
        was_alive = self._tmux.kill_session(tmux_name)

        lock_released = False
        if self._locks is not None:
            try:
                lock_released = self._locks.release(name)
            except Exception as exc:
                # Cosmetic — we already killed the pane. A failed
                # lock release just means a stale row that the next
                # ``/p`` or sweeper will clean up.
                self._log.warning(
                    "kill_lock_release_failed",
                    project=name,
                    error=str(exc),
                )

        self._log.info(
            "kill_kill",
            project=name,
            was_alive=was_alive,
            lock_released=lock_released,
        )
        return KillOutcome(
            project_name=name,
            was_alive=was_alive,
            lock_released=lock_released,
        )


__all__ = [
    "KillOutcome",
    "KillService",
    "StopOutcome",
    "TmuxError",
]
