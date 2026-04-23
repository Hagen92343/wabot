"""SessionService — lifecycle Use-Cases for Claude sessions in tmux.

Phase-4 C4.1d introduced ``ensure_started(project_name)``; C4.2c
adds ``send_prompt(project_name, text)`` which wraps the same
idempotent startup with injection sanitisation + Zero-Width-Space
bot-prefix (Spec §9) + tmux send_keys. ``on_turn_complete`` /
``recycle`` land in later C4.2+ checkpoints.

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

import time
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from whatsbot.application.transcript_ingest import TranscriptIngest
from whatsbot.domain.claude_paths import (
    DEFAULT_CLAUDE_HOME,
    claude_projects_dir,
    expected_transcript_path,
    extract_session_id,
    find_latest_transcript_since,
)
from whatsbot.domain.injection import sanitize
from whatsbot.domain.launch import build_claude_argv, render_command_line
from whatsbot.domain.modes import mode_badge, status_bar_color
from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import ClaudeSession, tmux_session_name
from whatsbot.domain.transcript import BOT_PREFIX
from whatsbot.logging_setup import get_logger
from whatsbot.ports.claude_session_repository import ClaudeSessionRepository
from whatsbot.ports.project_repository import ProjectRepository
from whatsbot.ports.tmux_controller import TmuxController
from whatsbot.ports.transcript_watcher import TranscriptWatcher, WatchHandle

# Default binary name — resolved by the shell inside the tmux pane. In
# prod the LaunchAgent puts ``~/whatsbot/bin`` on PATH; in tests the
# caller overrides this with an absolute path to the headless stub.
DEFAULT_SAFE_CLAUDE_BINARY = "safe-claude"

# Fresh-start transcript discovery: how long to wait for Claude to
# write its first event (and therefore create the ``<uuid>.jsonl``
# file), and how often to re-scan the projects dir while waiting.
# 2 s is enough for every local Claude Code launch measured so far;
# tests override to 0 to skip waiting.
DEFAULT_DISCOVERY_TIMEOUT_SECONDS: float = 2.0
DEFAULT_DISCOVERY_POLL_SECONDS: float = 0.05


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
        transcript_watcher: TranscriptWatcher | None = None,
        transcript_ingest: TranscriptIngest | None = None,
        claude_home: Path = DEFAULT_CLAUDE_HOME,
        discovery_timeout_seconds: float = DEFAULT_DISCOVERY_TIMEOUT_SECONDS,
        discovery_poll_seconds: float = DEFAULT_DISCOVERY_POLL_SECONDS,
    ) -> None:
        self._projects = project_repo
        self._sessions = session_repo
        self._tmux = tmux
        self._projects_root = projects_root
        self._safe_claude_binary = safe_claude_binary
        self._watcher = transcript_watcher
        self._ingest = transcript_ingest
        self._claude_home = claude_home
        self._discovery_timeout_s = discovery_timeout_seconds
        self._discovery_poll_s = discovery_poll_seconds
        # Tracks active transcript watches per project so ensure_started
        # is idempotent and we have a handle to pass to unwatch on
        # mode-recycle / shutdown.
        self._watches: dict[str, WatchHandle] = {}
        self._log = get_logger("whatsbot.sessions")

    # ---- public API ---------------------------------------------------

    def ensure_started(self, project_name: str) -> ClaudeSession:
        """Make sure the tmux session + Claude process for ``project_name``
        is up. Returns the (possibly freshly-created) ``ClaudeSession``
        record. Idempotent: calling twice in a row starts nothing the
        second time.

        If ``transcript_watcher`` + ``transcript_ingest`` were injected,
        this also attaches a per-project transcript watch so future
        Claude turns stream into the ingest pipeline.
        """
        project = self._projects.get(project_name)
        existing = self._sessions.get(project_name)
        tmux_name = tmux_session_name(project_name)
        project_path = self._projects_root / project_name

        tmux_alive = self._tmux.has_session(tmux_name)
        # Capture the launch checkpoint BEFORE we touch tmux so fresh-
        # start discovery only picks up transcripts Claude writes for
        # this run, not stale leftovers from a prior session.
        launch_checkpoint = time.time() - 1.0  # 1 s clock-skew tolerance

        if tmux_alive:
            # Nothing to launch — Claude is already running (or at least
            # tmux is). Refresh the status bar so a mode change since
            # the last ``ensure_started`` still shows through, and keep
            # the DB's current_mode column aligned with ``projects``.
            session = existing if existing is not None else self._fresh_record(project_name, project.mode)
            if existing is None:
                self._sessions.upsert(session)
            self._paint_status_bar(tmux_name, session.current_mode, project_name)
            # For the already-alive case we still want to (re-)attach
            # the transcript watch if the process lost it (e.g. bot
            # restart while tmux survived). Since the Claude process
            # has been running, the transcript file already exists —
            # no discovery polling needed if session_id is known, and
            # if it isn't we scan without a since_mtime filter.
            self._start_transcript_watch(
                project_name, since_mtime=None
            )
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
        self._start_transcript_watch(
            project_name, since_mtime=launch_checkpoint
        )
        return session

    def stop_transcript_watch(self, project_name: str) -> None:
        """Detach the transcript watch for ``project_name`` (idempotent).

        Called by C4.3's mode-recycle path before the tmux session is
        killed so no stale callbacks fire against the dead Claude.
        """
        handle = self._watches.pop(project_name, None)
        if handle is None or self._watcher is None:
            return
        self._watcher.unwatch(handle)

    def recycle(self, project_name: str) -> ClaudeSession:
        """Kill the tmux session, then relaunch via ``ensure_started``.

        Phase-4 C4.3 mode-switch path: the caller updates
        ``projects.mode`` (via ``ProjectRepository.update_mode``)
        *before* calling recycle so that ensure_started reads the new
        mode and builds the right ``safe-claude`` argv. The
        ``claude_sessions.session_id`` is preserved, so ``--resume``
        keeps the conversation context intact across the flag
        change.

        Also drops any in-flight transcript ingest state for the
        project (the new Claude process gets a clean buffer) and
        detaches the existing transcript watch — ensure_started
        re-attaches once the recycled Claude writes its first event.
        """
        self.stop_transcript_watch(project_name)
        if self._ingest is not None:
            self._ingest.reset(project_name)
        tmux_name = tmux_session_name(project_name)
        self._tmux.kill_session(tmux_name)
        return self.ensure_started(project_name)

    def send_prompt(self, project_name: str, text: str) -> None:
        """Deliver a user prompt into the project's Claude session.

        Steps:

        1. ``ensure_started`` — tmux + Claude are up, DB row present.
        2. Sanitize via ``domain.injection.sanitize``: if the prompt
           contains an injection telegraph *and* the project is in
           Normal mode, the text is wrapped in ``<untrusted_content>``
           tags. Strict and YOLO bypass the wrap (Spec §9).
        3. Prefix with Zero-Width-Space (Spec §9) so the transcript
           watcher can later distinguish bot-originated user turns
           from ones the human typed into the pane directly.
        4. ``tmux.send_text`` lands the prompt in the pane.

        The response — when Claude finishes its turn — is shipped
        to WhatsApp via the transcript-ingest path (C4.2d), not
        from this method. ``send_prompt`` returns once the prompt
        has been handed to tmux; the user sees an acknowledgement
        handled by the command layer.
        """
        session = self.ensure_started(project_name)

        result = sanitize(text, mode=session.current_mode)
        if result.suspected:
            # Mirror the format already emitted by the /webhook
            # middleware so audit reviews have one coherent event
            # shape. ``mode`` tells forensics whether the wrap
            # actually took effect (Normal) or was bypassed
            # (Strict/YOLO).
            self._log.warning(
                "injection_suspected",
                project=project_name,
                mode=session.current_mode.value,
                triggers=list(result.triggers),
                wrapped=(result.text != text),
                text_len=len(text),
            )

        prefixed = BOT_PREFIX + result.text
        tmux_name = tmux_session_name(project_name)
        self._tmux.send_text(tmux_name, prefixed)

        self._log.info(
            "prompt_sent",
            project=project_name,
            mode=session.current_mode.value,
            tmux_session=tmux_name,
            text_len=len(text),
            injection_suspected=result.suspected,
        )

    # ---- transcript watching ------------------------------------------

    def _start_transcript_watch(
        self, project_name: str, *, since_mtime: float | None
    ) -> None:
        """Attach a ``TranscriptWatcher`` to the project's transcript.

        Idempotent — duplicate calls while a watch is already active
        are no-ops. When ``transcript_watcher`` / ``transcript_ingest``
        aren't injected, this is a no-op so older call paths (and
        tests that don't care about transcripts) keep working.

        ``since_mtime=None`` disables the freshness filter — used on
        the "tmux already alive" code path where we want to pick up
        the existing transcript whatever its age. For the fresh
        launch path we pass ``time.time() - tolerance`` so stale
        transcripts from prior sessions are ignored.
        """
        if self._watcher is None or self._ingest is None:
            return
        if project_name in self._watches:
            return
        session = self._sessions.get(project_name)
        if session is None:
            return  # defensive — shouldn't happen, ensure_started just wrote it

        project_cwd = self._projects_root / project_name
        if session.session_id:
            # Resume path: we know the exact filename.
            transcript_path = expected_transcript_path(
                project_cwd,
                session.session_id,
                claude_home=self._claude_home,
            )
        else:
            # Fresh path: poll the projects dir for a newly-created .jsonl.
            projects_dir = claude_projects_dir(
                project_cwd, claude_home=self._claude_home
            )
            discovered = self._poll_for_transcript(
                projects_dir, since_mtime=since_mtime
            )
            if discovered is None:
                self._log.warning(
                    "transcript_discovery_timeout",
                    project=project_name,
                    timeout_seconds=self._discovery_timeout_s,
                )
                return
            transcript_path = discovered
            # Persist the discovered session_id + transcript path so
            # a subsequent ensure_started can take the resume path.
            session = replace(
                session,
                session_id=extract_session_id(transcript_path),
                transcript_path=str(transcript_path),
            )
            self._sessions.upsert(session)

        watcher = self._watcher
        ingest = self._ingest

        def _forward(line: str) -> None:
            ingest.feed(project_name, line)

        handle = watcher.watch(transcript_path, _forward)
        self._watches[project_name] = handle
        self._log.info(
            "transcript_watch_attached",
            project=project_name,
            path=str(transcript_path),
            session_id=session.session_id,
        )

    def _poll_for_transcript(
        self, projects_dir: Path, *, since_mtime: float | None
    ) -> Path | None:
        """Synchronously wait for the first ``*.jsonl`` to appear.

        Blocks the caller thread up to ``discovery_timeout_seconds``.
        Fine for a single-user bot where ``ensure_started`` is called
        from the webhook request handler — we accept the couple-hundred-
        millisecond wait to avoid the complexity of an async
        discovery thread.
        """
        deadline = time.monotonic() + self._discovery_timeout_s
        while True:
            found = find_latest_transcript_since(
                projects_dir, since_mtime=since_mtime
            )
            if found is not None:
                return found
            if time.monotonic() >= deadline:
                return None
            time.sleep(self._discovery_poll_s)

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
