"""StartupRecovery — coerce YOLO → Normal + resume sessions after a reboot.

Spec §6 + §8 + phase-4.md C4.6 + C4.7. The LaunchAgent restarts
the bot after login (or after a process crash). This service owns
the "first thing we do after the event loop is up" logic:

1. **YOLO reset**: any project whose mode is ``yolo`` is flipped
   to ``normal`` in ``projects``, and a ``mode_events`` row with
   ``event='reboot_reset'`` is appended. Spec §6 requires this —
   YOLO is explicitly session-scoped; a reboot must not leave
   ``--dangerously-skip-permissions`` armed without deliberate
   user intent.

2. **Session restore**: every row in ``claude_sessions`` is fed
   through ``SessionService.ensure_started``. For rows with a
   known ``session_id``, the resume path fires — ``safe-claude
   --resume <id>`` picks up the prior transcript + context. For
   rows without a session_id (rare — transcript never wrote),
   the fresh launch path runs.

Order matters: YOLO reset must land in the DB *before*
restore_sessions calls ensure_started, so the relaunched Claude
sees the coerced-Normal flag set on projects.mode.

Failures during a single project's restore don't abort the whole
recovery — we log and continue. The user will see the sessions
that came back; broken ones get a manual ``/p <name>`` retry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ulid import ULID

from whatsbot.application.session_service import SessionService
from whatsbot.domain.mode_events import ModeEvent, ModeEventKind
from whatsbot.domain.projects import Mode
from whatsbot.logging_setup import get_logger
from whatsbot.ports.claude_session_repository import ClaudeSessionRepository
from whatsbot.ports.mode_event_repository import ModeEventRepository
from whatsbot.ports.project_repository import ProjectRepository


@dataclass(frozen=True, slots=True)
class RecoveryReport:
    """Summary of one ``StartupRecovery.run`` invocation.

    Surfaced through logs + ``/status`` so the user knows what
    the bot brought back after a restart.
    """

    yolo_resets: tuple[str, ...]
    restored_sessions: tuple[str, ...]
    failed_sessions: tuple[str, ...]


class StartupRecovery:
    def __init__(
        self,
        *,
        project_repo: ProjectRepository,
        session_repo: ClaudeSessionRepository,
        mode_event_repo: ModeEventRepository,
        session_service: SessionService,
    ) -> None:
        self._projects = project_repo
        self._sessions = session_repo
        self._events = mode_event_repo
        self._session_service = session_service
        self._log = get_logger("whatsbot.startup_recovery")

    def run(self) -> RecoveryReport:
        """Coerce YOLO, then restore sessions. Order matters."""
        resets = self.reset_yolo_to_normal()
        restored, failed = self.restore_sessions()
        self._log.info(
            "startup_recovery_complete",
            yolo_resets=list(resets),
            restored_sessions=list(restored),
            failed_sessions=list(failed),
        )
        return RecoveryReport(
            yolo_resets=resets,
            restored_sessions=restored,
            failed_sessions=failed,
        )

    # ---- steps --------------------------------------------------------

    def reset_yolo_to_normal(self) -> tuple[str, ...]:
        """Flip every YOLO project to Normal and append an audit row.

        Returns the list of project names that were reset — empty
        tuple when no YOLO projects exist.
        """
        resets: list[str] = []
        for project in self._projects.list_all():
            if project.mode is not Mode.YOLO:
                continue
            self._projects.update_mode(project.name, Mode.NORMAL)
            # claude_sessions.current_mode gets realigned by
            # SessionService.ensure_started during restore; we don't
            # pre-update it here because the session row may not
            # exist for every YOLO project.
            self._record_reboot_reset(project.name)
            resets.append(project.name)
        if resets:
            self._log.info("yolo_projects_reset", projects=resets)
        return tuple(resets)

    def restore_sessions(self) -> tuple[tuple[str, ...], tuple[str, ...]]:
        """Call ``ensure_started`` for each ``claude_sessions`` row.

        Returns (restored, failed) — two tuples of project names.
        Failures are logged but don't abort the loop; the bot
        stays up with whatever sessions came back cleanly.
        """
        restored: list[str] = []
        failed: list[str] = []
        for row in self._sessions.list_all():
            try:
                self._session_service.ensure_started(row.project_name)
                restored.append(row.project_name)
            except Exception as exc:  # pragma: no cover - logged
                self._log.exception(
                    "session_restore_failed",
                    project=row.project_name,
                    error=str(exc),
                )
                failed.append(row.project_name)
        return tuple(restored), tuple(failed)

    # ---- internals ----------------------------------------------------

    def _record_reboot_reset(self, project_name: str) -> None:
        event = ModeEvent(
            id=str(ULID()),
            project_name=project_name,
            kind=ModeEventKind.REBOOT_RESET,
            from_mode=Mode.YOLO,
            to_mode=Mode.NORMAL,
            at=datetime.now(UTC),
            msg_id=None,
        )
        try:
            self._events.record(event)
        except Exception:  # pragma: no cover - logged
            # Audit-log write failures never cancel the reset —
            # Spec §26 accepts the audit log is not append-only
            # enforced. Log and move on.
            self._log.exception(
                "reboot_reset_audit_failed", project=project_name
            )
