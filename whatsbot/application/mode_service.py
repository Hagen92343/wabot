"""ModeService — switch a project's permission mode.

Spec §6 defines three per-project modes: ``normal``, ``strict``,
``yolo``. ``/mode <name>`` user-facing command goes through this
service; reboot-reset + panic-reset come in C4.7 / Phase 6 via
their own entry points but will reuse this same state transition.

Flow of ``change_mode(project, new_mode)``:

1. Validate the transition (``domain.modes.valid_transition``) — a
   same-mode switch is a legal no-op that still returns success.
2. Update the ``projects`` row so the next ``ensure_started`` reads
   the new mode.
3. Update the ``claude_sessions.current_mode`` column (partial
   update — no other columns touched).
4. Append a ``mode_events`` audit row.
5. Recycle the tmux/Claude session (unless it wasn't running) so
   Claude is re-launched with the mode-specific CLI flag. The
   session_id is preserved via ``--resume`` so context survives.

The service is intentionally small — the hard work lives in
``SessionService.recycle`` + the repositories. It exists as its
own service so the audit-write and the recycle stay atomic from
the caller's perspective (one call, one transaction semantic).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ulid import ULID

from whatsbot.application.session_service import SessionService
from whatsbot.domain.mode_events import ModeEvent, ModeEventKind
from whatsbot.domain.modes import valid_transition
from whatsbot.domain.projects import Mode
from whatsbot.logging_setup import get_logger
from whatsbot.ports.claude_session_repository import ClaudeSessionRepository
from whatsbot.ports.mode_event_repository import ModeEventRepository
from whatsbot.ports.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)


class InvalidModeTransitionError(ValueError):
    """Raised when ``valid_transition`` rejects a switch (currently
    none — but the exception type is here so callers don't need to
    add one later)."""


@dataclass(frozen=True, slots=True)
class ModeChangeOutcome:
    """Return value of ``change_mode``. ``was_noop`` is True when the
    project was already in the target mode — the command handler
    phrases the reply differently in that case."""

    project_name: str
    from_mode: Mode
    to_mode: Mode
    was_noop: bool


class ModeService:
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
        self._log = get_logger("whatsbot.modes")

    def change_mode(
        self,
        project_name: str,
        new_mode: Mode,
        *,
        msg_id: str | None = None,
    ) -> ModeChangeOutcome:
        project = self._projects.get(project_name)
        from_mode = project.mode

        if not valid_transition(from_mode, new_mode):
            raise InvalidModeTransitionError(
                f"Wechsel von {from_mode.value} nach {new_mode.value} nicht erlaubt."
            )

        if from_mode == new_mode:
            # No-op — still record an audit row so grep'ing the log
            # tells the whole story, but skip the recycle since the
            # existing process already has the right flags.
            self._record_event(project_name, from_mode, new_mode, msg_id)
            self._log.info(
                "mode_switch_noop",
                project=project_name,
                mode=new_mode.value,
            )
            return ModeChangeOutcome(
                project_name=project_name,
                from_mode=from_mode,
                to_mode=new_mode,
                was_noop=True,
            )

        # Order matters: projects.mode must be updated BEFORE
        # session_service.recycle so the ensure_started path inside
        # recycle reads the new value and builds the right argv.
        self._projects.update_mode(project_name, new_mode)
        # claude_sessions.current_mode may or may not exist yet (a
        # brand-new project hasn't launched Claude yet). Update
        # either way — the SQLite UPDATE is a no-op if no row.
        self._sessions.update_mode(project_name, new_mode)
        self._record_event(project_name, from_mode, new_mode, msg_id)

        self._session_service.recycle(project_name)

        self._log.info(
            "mode_switch_completed",
            project=project_name,
            from_mode=from_mode.value,
            to_mode=new_mode.value,
            msg_id=msg_id,
        )
        return ModeChangeOutcome(
            project_name=project_name,
            from_mode=from_mode,
            to_mode=new_mode,
            was_noop=False,
        )

    def show_mode(self, project_name: str) -> Mode:
        """Return the current mode for ``project_name``. Raises
        ``ProjectNotFoundError`` — the command handler turns that
        into the usual ``/ls`` hint reply."""
        project = self._projects.get(project_name)
        return project.mode

    # ---- internals ----------------------------------------------------

    def _record_event(
        self,
        project_name: str,
        from_mode: Mode,
        to_mode: Mode,
        msg_id: str | None,
    ) -> None:
        event = ModeEvent(
            id=str(ULID()),
            project_name=project_name,
            kind=ModeEventKind.SWITCH,
            from_mode=from_mode,
            to_mode=to_mode,
            at=datetime.now(UTC),
            msg_id=msg_id,
        )
        try:
            self._events.record(event)
        except Exception:  # pragma: no cover - logged, never raised
            # Audit-log hiccups must not cancel a successful mode
            # switch (Spec §26 acknowledges the audit log is not
            # append-only-enforced). Log and move on.
            self._log.exception(
                "mode_event_record_failed", project=project_name
            )


# Surface ProjectNotFoundError in this module so the command handler
# can ``except`` against one import line.
__all__ = [
    "InvalidModeTransitionError",
    "ModeChangeOutcome",
    "ModeService",
    "ProjectNotFoundError",
]
