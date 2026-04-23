"""SessionService — lifecycle Use-Cases for Claude sessions in tmux.

Phase-4 C4.1d scope: ``ensure_started(project_name)``. That's the one
entry point ``/p <name>`` needs right now. Recycle, send_prompt,
on_turn_complete land in later checkpoints (C4.2 ff.).

Flow of ``ensure_started``:

1. Look up the ``projects`` row for ``mode`` — if the project has no
   row, raise; ``/p`` should have been guarded by ``ActiveProjectService``
   before we got here, but we defend for ordering.
2. Look up the ``claude_sessions`` row. If it exists with a non-empty
   ``session_id``, we'll start Claude with ``--resume <id>``; otherwise
   we start fresh. Either way we ensure a row is present afterwards
   so Phase 4's transcript-watcher has somewhere to stash
   ``transcript_path`` + running token totals later.
3. If the tmux session doesn't exist, create it rooted at the project's
   on-disk path and ``send_text`` the ``safe-claude ...`` command into
   the new pane.
4. Refresh the tmux status bar with the mode-specific colour + label
   so the user sees the right emoji immediately.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from whatsbot.domain.launch import build_claude_argv, render_command_line
from whatsbot.domain.modes import mode_badge, status_bar_color
from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import ClaudeSession, tmux_session_name
from whatsbot.logging_setup import get_logger
from whatsbot.ports.claude_session_repository import ClaudeSessionRepository
from whatsbot.ports.project_repository import ProjectRepository
from whatsbot.ports.tmux_controller import TmuxController

# Default binary name — resolved by the shell inside the tmux pane. In
# prod the LaunchAgent puts ``~/whatsbot/bin`` on PATH; in tests the
# caller overrides this with an absolute path to the headless stub.
DEFAULT_SAFE_CLAUDE_BINARY = "safe-claude"


class SessionService:
    """Orchestrates tmux + claude_sessions bookkeeping."""

    def __init__(
        self,
        *,
        project_repo: ProjectRepository,
        session_repo: ClaudeSessionRepository,
        tmux: TmuxController,
        projects_root: Path,
        safe_claude_binary: str = DEFAULT_SAFE_CLAUDE_BINARY,
    ) -> None:
        self._projects = project_repo
        self._sessions = session_repo
        self._tmux = tmux
        self._projects_root = projects_root
        self._safe_claude_binary = safe_claude_binary
        self._log = get_logger("whatsbot.sessions")

    # ---- public API ---------------------------------------------------

    def ensure_started(self, project_name: str) -> ClaudeSession:
        """Make sure the tmux session + Claude process for ``project_name``
        is up. Returns the (possibly freshly-created) ``ClaudeSession``
        record. Idempotent: calling twice in a row starts nothing the
        second time.
        """
        project = self._projects.get(project_name)
        existing = self._sessions.get(project_name)
        tmux_name = tmux_session_name(project_name)
        project_path = self._projects_root / project_name

        tmux_alive = self._tmux.has_session(tmux_name)

        if tmux_alive:
            # Nothing to launch — Claude is already running (or at least
            # tmux is). Refresh the status bar so a mode change since
            # the last ``ensure_started`` still shows through, and keep
            # the DB's current_mode column aligned with ``projects``.
            session = existing if existing is not None else self._fresh_record(project_name, project.mode)
            if existing is None:
                self._sessions.upsert(session)
            self._paint_status_bar(tmux_name, session.current_mode, project_name)
            return session

        # tmux missing → start. If there's a DB row from a previous run,
        # honour its session_id so --resume picks up the prior transcript.
        session = existing if existing is not None else self._fresh_record(project_name, project.mode)

        # Align the session record's mode with the project row — the
        # project might have switched modes while tmux wasn't running.
        if session.current_mode != project.mode:
            session = session.with_mode(project.mode)

        self._tmux.new_session(tmux_name, cwd=project_path)
        argv = build_claude_argv(
            safe_claude_binary=self._safe_claude_binary,
            session_id=session.session_id,
            mode=project.mode,
        )
        self._tmux.send_text(tmux_name, render_command_line(argv))
        self._paint_status_bar(tmux_name, project.mode, project_name)

        # Persist after the launch so a failed new_session doesn't leave
        # a stale row behind. upsert is cheap — a single INSERT ... ON
        # CONFLICT under the hood.
        self._sessions.upsert(session)

        self._log.info(
            "claude_session_started",
            project=project_name,
            mode=project.mode.value,
            tmux_session=tmux_name,
            resumed=bool(session.session_id),
        )
        return session

    # ---- internals ----------------------------------------------------

    def _fresh_record(self, project_name: str, mode: Mode) -> ClaudeSession:
        """Build an in-memory ClaudeSession row for a first launch.
        ``session_id`` + ``transcript_path`` stay empty — the transcript
        watcher (C4.2) fills them once Claude writes its first line.
        """
        return ClaudeSession(
            project_name=project_name,
            session_id="",
            transcript_path="",
            started_at=datetime.now(UTC),
            current_mode=mode,
        )

    def _paint_status_bar(
        self, tmux_name: str, mode: Mode, project_name: str
    ) -> None:
        del project_name  # currently unused; kept for future label layout
        label = f"{mode_badge(mode)} [{tmux_name}]"
        self._tmux.set_status(
            tmux_name,
            color=status_bar_color(mode),
            label=label,
        )
