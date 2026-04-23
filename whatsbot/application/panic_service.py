"""PanicService — Phase 6 ``/panic`` orchestrator.

Spec §7 / §11 / §21 Phase 6 C6.2 + C6.3. The user types ``/panic``
and we run a fixed 6-step playbook in this exact order:

1. **Engage lockdown** (``LockdownService.engage`` writes the DB row
   *and* the touch-file ``/tmp/whatsbot-PANIC``). Doing this *first*
   means a concurrent webhook that arrives mid-panic can't restart
   anything we've already torn down.
2. **Enumerate every ``wb-*`` tmux session** and ``kill_session`` it
   one by one. tmux's SIGHUP cascade triggers Claude to exit
   gracefully on most cases.
3. **`pkill` backstop** for any Claude process that didn't exit on
   its own. We use a narrow pattern (default ``"safe-claude"``) to
   avoid nuking foreign Claude installs on the same machine.
4. **YOLO → Normal** for every project still in ``yolo`` mode, with
   a ``mode_events.event='panic_reset'`` audit row per project.
   Spec §6 invariant: YOLO must never survive a kill switch.
5. **Release every lock** so the ``BOT``-marker doesn't outlive the
   dead Claude. Bot session state is gone; the locks would be
   misleading otherwise.
6. **Notify** the user on the local desktop so panics don't hide
   when they happen while no one is at the keyboard.

PanicService deliberately does **not** wipe the ``claude_sessions``
rows — the bot needs them on recovery to ``--resume`` once
``/unlock`` runs. Use ``/rm <name> <PIN>`` if the user actually wants
to forget the project.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from ulid import ULID

from whatsbot.application.lock_service import LockService
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.domain.lockdown import LOCKDOWN_REASON_PANIC, LockdownState
from whatsbot.domain.mode_events import ModeEvent, ModeEventKind
from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import tmux_session_name
from whatsbot.logging_setup import get_logger
from whatsbot.ports.mode_event_repository import ModeEventRepository
from whatsbot.ports.notification_sender import NotificationSender
from whatsbot.ports.process_killer import (
    KillResult,
    ProcessKiller,
    ProcessKillerError,
)
from whatsbot.ports.project_repository import ProjectRepository
from whatsbot.ports.tmux_controller import TmuxController, TmuxError

# Default ``pkill -f`` pattern. Matches the bot's safe-claude wrapper
# binary so foreign ``claude`` installs run by the same user are not
# affected (Spec §21 Phase 6 abbruch-criterion).
DEFAULT_CLAUDE_PROCESS_PATTERN = "safe-claude"

# wb-* prefix from Spec §7 — every bot-managed tmux session name.
TMUX_SESSION_PREFIX = "wb-"


@dataclass(frozen=True, slots=True)
class PanicOutcome:
    """Structured result of a panic run, for the WhatsApp ack and
    audit log."""

    sessions_killed: tuple[str, ...]
    yolo_projects_reset: tuple[str, ...]
    locks_released: tuple[str, ...]
    process_killer_result: KillResult | None
    lockdown_state: LockdownState
    duration_seconds: float


class PanicService:
    """One method: ``panic()``. Wires the 6-step Spec §7 playbook."""

    def __init__(
        self,
        *,
        tmux: TmuxController,
        project_repo: ProjectRepository,
        mode_event_repo: ModeEventRepository,
        lock_service: LockService,
        lockdown_service: LockdownService,
        process_killer: ProcessKiller,
        notifier: NotificationSender | None = None,
        claude_pattern: str = DEFAULT_CLAUDE_PROCESS_PATTERN,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._tmux = tmux
        self._projects = project_repo
        self._events = mode_event_repo
        self._locks = lock_service
        self._lockdown = lockdown_service
        self._killer = process_killer
        self._notifier = notifier
        self._claude_pattern = claude_pattern
        self._clock = clock
        self._monotonic = monotonic
        self._log = get_logger("whatsbot.panic")

    def panic(self) -> PanicOutcome:
        """Run the full 6-step panic playbook. Idempotent."""
        started = self._monotonic()

        # 1. Lockdown FIRST so concurrent webhooks can't recreate
        #    sessions while we're tearing them down.
        lockdown_state = self._lockdown.engage(
            reason=LOCKDOWN_REASON_PANIC,
            engaged_by="panic",
        )

        # 2. tmux kill-session per wb-* session.
        sessions_killed = self._kill_all_wb_sessions()

        # 3. pkill backstop for stuck claudes.
        kill_result = self._run_process_killer()

        # 4. YOLO → Normal for every project still YOLO.
        yolo_resets = self._reset_yolo_projects()

        # 5. Release every lock — both the killed-session locks and
        #    any stale rows on projects that didn't have a tmux
        #    session running when panic landed.
        locks_released = self._release_all_locks()

        # 6. macOS-style notification (no-op on Linux / missing
        #    binary).
        elapsed = self._monotonic() - started
        if self._notifier is not None:
            self._send_notification(
                sessions_killed=sessions_killed,
                yolo_resets=yolo_resets,
                elapsed=elapsed,
            )

        outcome = PanicOutcome(
            sessions_killed=sessions_killed,
            yolo_projects_reset=yolo_resets,
            locks_released=locks_released,
            process_killer_result=kill_result,
            lockdown_state=lockdown_state,
            duration_seconds=elapsed,
        )
        self._log.warning(
            "panic_engaged",
            sessions=list(sessions_killed),
            yolo_resets=list(yolo_resets),
            locks_released=list(locks_released),
            duration_ms=round(elapsed * 1000, 1),
        )
        return outcome

    # ---- internals --------------------------------------------------

    def _kill_all_wb_sessions(self) -> tuple[str, ...]:
        try:
            sessions = self._tmux.list_sessions(prefix=TMUX_SESSION_PREFIX)
        except TmuxError as exc:
            self._log.warning("panic_list_sessions_failed", error=str(exc))
            return ()
        killed: list[str] = []
        for tmux_name in sessions:
            try:
                if self._tmux.kill_session(tmux_name):
                    killed.append(tmux_name)
            except TmuxError as exc:
                self._log.warning(
                    "panic_kill_session_failed",
                    tmux_session=tmux_name,
                    error=str(exc),
                )
        return tuple(killed)

    def _run_process_killer(self) -> KillResult | None:
        try:
            return self._killer.kill_by_pattern(self._claude_pattern)
        except ProcessKillerError as exc:
            self._log.warning(
                "panic_process_kill_failed",
                pattern=self._claude_pattern,
                error=str(exc),
            )
            return None

    def _reset_yolo_projects(self) -> tuple[str, ...]:
        resets: list[str] = []
        now = self._clock()
        for project in self._projects.list_all():
            if project.mode is not Mode.YOLO:
                continue
            try:
                self._projects.update_mode(project.name, Mode.NORMAL)
            except Exception as exc:
                self._log.warning(
                    "panic_yolo_reset_failed",
                    project=project.name,
                    error=str(exc),
                )
                continue
            self._record_panic_reset(project.name, now=now)
            resets.append(project.name)
        return tuple(resets)

    def _release_all_locks(self) -> tuple[str, ...]:
        released: list[str] = []
        # We need to iterate over all known projects, not just the
        # killed sessions: a project can hold a lock without an
        # active tmux pane (e.g. local user typing in a stale local
        # lock that never auto-released).
        project_names = {p.name for p in self._projects.list_all()}
        # Add killed-session names defensively in case the project
        # row was deleted but a stale lock somehow survived FK
        # cascade (shouldn't happen — but cheap to be safe).
        for tmux_name in self._tmux.list_sessions(
            prefix=TMUX_SESSION_PREFIX
        ):
            project_names.add(_tmux_to_project_name(tmux_name))
        for name in sorted(project_names):
            try:
                if self._locks.release(name):
                    released.append(name)
            except Exception as exc:
                self._log.warning(
                    "panic_lock_release_failed",
                    project=name,
                    error=str(exc),
                )
        return tuple(released)

    def _record_panic_reset(self, project_name: str, *, now: datetime) -> None:
        event = ModeEvent(
            id=str(ULID()),
            project_name=project_name,
            kind=ModeEventKind.PANIC_RESET,
            from_mode=Mode.YOLO,
            to_mode=Mode.NORMAL,
            at=now,
            msg_id=None,
        )
        try:
            self._events.record(event)
        except Exception as exc:
            # Audit log isn't append-only-enforced (Spec §26).
            # Log and move on; the actual mode update is what
            # matters for the kill-switch invariant.
            self._log.warning(
                "panic_reset_audit_failed",
                project=project_name,
                error=str(exc),
            )

    def _send_notification(
        self,
        *,
        sessions_killed: tuple[str, ...],
        yolo_resets: tuple[str, ...],
        elapsed: float,
    ) -> None:
        assert self._notifier is not None  # for type checker
        body_parts = [
            f"{len(sessions_killed)} Sessions getötet",
        ]
        if yolo_resets:
            body_parts.append(
                f"{len(yolo_resets)} YOLO → Normal"
            )
        body_parts.append(f"{elapsed * 1000:.0f} ms")
        body = ", ".join(body_parts)
        try:
            self._notifier.send(
                title="🚨 whatsbot PANIC engaged",
                body=body,
                sound=True,
            )
        except Exception as exc:
            self._log.warning(
                "panic_notify_failed", error=str(exc)
            )


def _tmux_to_project_name(tmux_name: str) -> str:
    """Inverse of ``domain.sessions.tmux_session_name``.

    Used here so we can map ``wb-foo`` → ``foo`` when we want to
    release the lock on a project we only know by its tmux session
    name. Defensive against missing-prefix rows.
    """
    if tmux_name.startswith(TMUX_SESSION_PREFIX):
        return tmux_name[len(TMUX_SESSION_PREFIX) :]
    return tmux_name


def _expected_tmux_name(project_name: str) -> str:
    """Public helper for tests so they can build the expected name
    without re-importing ``tmux_session_name``."""
    return tmux_session_name(project_name)
