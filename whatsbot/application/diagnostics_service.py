"""DiagnosticsService — read-only backend for ``/log``, ``/errors``, ``/ps``.

Spec §11 + §15. Phase 8 C8.2 wiring:

* :meth:`read_trace` — full event chain for one inbound message
  keyed by ``msg_id``.
* :meth:`recent_errors` — last N error/warning-level events.
* :meth:`active_sessions` — Claude sessions the bot believes are
  live (tmux says so), with mode, token-fill, lock-owner badge.

The service is pure-read — no DB writes, no tmux side-effects.
All formatting is done here so :class:`CommandHandler` can
delegate without knowing the log shape.
"""

from __future__ import annotations

from dataclasses import dataclass

from whatsbot.domain.locks import LockOwner, lock_owner_badge
from whatsbot.domain.log_events import (
    LogEntry,
    filter_by_msg_id,
    filter_errors,
)
from whatsbot.domain.modes import mode_badge
from whatsbot.domain.projects import Mode
from whatsbot.logging_setup import get_logger
from whatsbot.ports.claude_session_repository import ClaudeSessionRepository
from whatsbot.ports.log_reader import LogReader
from whatsbot.ports.session_lock_repository import SessionLockRepository
from whatsbot.ports.tmux_controller import TmuxController, TmuxError

# Spec §15 logs cap at 10 MB x 5 backup files per sink. A single
# line is ~200-500 bytes in the JSON format, so 2 000 lines is
# roughly the most recent 500 KB - plenty for /log + /errors and
# still bounded.
DEFAULT_TAIL_LIMIT: int = 2000

# Safety cap on /log output — if a single msg_id's trace is long
# enough to exceed the OutputService oversized threshold
# (Spec §10), the command handler will route it through the
# ``/send``/``/discard``/``/save`` pipeline. Here we just cap
# the number of events rendered so a runaway trace doesn't
# allocate unbounded strings.
MAX_TRACE_EVENTS: int = 200

# ``/errors`` default window — Spec §11 names 10 explicitly.
DEFAULT_ERRORS_LIMIT: int = 10


@dataclass(frozen=True, slots=True)
class SessionSnapshot:
    """What ``/ps`` shows per running session."""

    project_name: str
    mode: Mode
    tmux_alive: bool
    turns_count: int
    tokens_used: int
    context_fill_ratio: float
    lock_owner: LockOwner


class DiagnosticsService:
    """Read-only diagnostics surface used by the Phase-8 commands."""

    def __init__(
        self,
        *,
        log_reader: LogReader,
        claude_sessions: ClaudeSessionRepository | None = None,
        locks: SessionLockRepository | None = None,
        tmux: TmuxController | None = None,
        tail_limit: int = DEFAULT_TAIL_LIMIT,
    ) -> None:
        self._logs = log_reader
        self._claude_sessions = claude_sessions
        self._locks = locks
        self._tmux = tmux
        self._tail_limit = max(tail_limit, 1)
        self._log = get_logger("whatsbot.diagnostics")

    # ---- /log <msg_id> ----------------------------------------------

    def read_trace(self, msg_id: str) -> list[LogEntry]:
        """Return all log events tagged with ``msg_id``, chronological.

        The tail is already ordered newest-last in the file; we
        keep that order when filtering. Capped at
        :data:`MAX_TRACE_EVENTS` so a huge trace doesn't blow up
        the WhatsApp body.
        """
        tail = self._logs.read_tail(max_lines=self._tail_limit)
        filtered = filter_by_msg_id(tail, msg_id)
        if len(filtered) > MAX_TRACE_EVENTS:
            # Keep the most recent MAX_TRACE_EVENTS — that's what
            # the user is most likely asking about ("what just
            # happened?"). Older events are still accessible in
            # the raw JSONL on disk.
            filtered = filtered[-MAX_TRACE_EVENTS:]
        return filtered

    def format_trace(self, msg_id: str, entries: list[LogEntry]) -> str:
        """Render a trace block for WhatsApp. Single source of truth
        for the user-facing format so CommandHandler stays lean."""
        if not entries:
            return f"kein Trace für msg_id={msg_id}."
        lines = [f"Trace msg_id={msg_id} ({len(entries)} Events):"]
        for entry in entries:
            lines.append(_format_entry_line(entry))
        return "\n".join(lines)

    # ---- /errors -----------------------------------------------------

    def recent_errors(self, *, limit: int = DEFAULT_ERRORS_LIMIT) -> list[LogEntry]:
        if limit <= 0:
            return []
        tail = self._logs.read_tail(max_lines=self._tail_limit)
        errors = filter_errors(tail)
        if len(errors) > limit:
            return errors[-limit:]
        return errors

    def format_errors(self, entries: list[LogEntry]) -> str:
        if not entries:
            return "keine Fehler in den letzten Events 🎉"
        lines = [f"Letzte {len(entries)} Fehler:"]
        for entry in entries:
            lines.append(_format_entry_line(entry))
        return "\n".join(lines)

    # ---- /ps --------------------------------------------------------

    def active_sessions(self) -> list[SessionSnapshot]:
        """Session list from the DB, joined with tmux liveness +
        lock owner. If either port was never wired (no tmux in TEST
        env) we return ``[]`` — the command handler renders that as
        "keine aktiven Sessions" which is semantically right."""
        if self._claude_sessions is None:
            return []

        try:
            rows = self._claude_sessions.list_all()
        except Exception as exc:  # pragma: no cover — defensive
            self._log.warning("diagnostics_list_sessions_failed", error=str(exc))
            return []

        if not rows:
            return []

        alive_names = self._tmux_live_names()

        snapshots: list[SessionSnapshot] = []
        for row in rows:
            owner = self._lock_owner(row.project_name)
            snapshots.append(
                SessionSnapshot(
                    project_name=row.project_name,
                    mode=row.current_mode,
                    tmux_alive=_tmux_name_for(row.project_name) in alive_names,
                    turns_count=row.turns_count,
                    tokens_used=row.tokens_used,
                    context_fill_ratio=row.context_fill_ratio,
                    lock_owner=owner,
                )
            )
        return snapshots

    def format_sessions(self, snapshots: list[SessionSnapshot]) -> str:
        if not snapshots:
            return "keine aktiven Sessions."
        lines = ["Aktive Sessions:"]
        for snap in snapshots:
            mode_label = mode_badge(snap.mode)
            owner_label = lock_owner_badge(snap.lock_owner)
            pct = int(round(snap.context_fill_ratio * 100))
            alive = "🟢" if snap.tmux_alive else "⚫"
            lines.append(
                f"{alive} {snap.project_name} · {mode_label} · {owner_label}"
                f" · turn {snap.turns_count} · ctx {pct}%"
                f" · {snap.tokens_used} tok"
            )
        return "\n".join(lines)

    # ---- /update -----------------------------------------------------

    def format_update_hint(self) -> str:
        """Spec §21 Phase 8 + §22: Claude Code updates are explicitly
        manual (the vierfache Subscription-Lock from §5 forbids
        silent auto-upgrades). We just explain the procedure."""
        return (
            "Claude-Code-Updates laufen manuell:\n"
            "  1. Am Mac: ./install-claude-code.sh\n"
            "  2. claude /status bestätigt: 'subscription', nicht 'API'\n"
            "  3. launchctl kickstart com.<domain>.whatsbot\n"
            "  4. Sessions werden via --resume wiederhergestellt\n"
            "  Details: docs/RUNBOOK.md §Update"
        )

    # ---- internals --------------------------------------------------

    def _tmux_live_names(self) -> frozenset[str]:
        if self._tmux is None:
            return frozenset()
        try:
            names = self._tmux.list_sessions(prefix="wb-")
        except TmuxError as exc:
            self._log.warning("diagnostics_tmux_list_failed", error=str(exc))
            return frozenset()
        return frozenset(names)

    def _lock_owner(self, project_name: str) -> LockOwner:
        if self._locks is None:
            return LockOwner.FREE
        try:
            row = self._locks.get(project_name)
        except Exception as exc:  # pragma: no cover — defensive
            self._log.warning(
                "diagnostics_lock_read_failed",
                project=project_name,
                error=str(exc),
            )
            return LockOwner.FREE
        return row.owner if row is not None else LockOwner.FREE


def _tmux_name_for(project_name: str) -> str:
    return f"wb-{project_name}"


def _format_entry_line(entry: LogEntry) -> str:
    """One log-entry line as it appears in ``/log`` / ``/errors``."""
    ts = entry.ts or "?"
    level = entry.level.upper() or "?"
    event = entry.event or "(no event)"
    bits = [f"{ts} {level} {event}"]

    if entry.project:
        bits.append(f"project={entry.project}")
    if entry.mode:
        bits.append(f"mode={entry.mode}")
    if entry.msg_id and entry.msg_id not in bits[0]:
        # /errors shows msg_id for triage.
        bits.append(f"msg_id={entry.msg_id}")
    return " · ".join(bits)


__all__ = [
    "DEFAULT_ERRORS_LIMIT",
    "DEFAULT_TAIL_LIMIT",
    "DiagnosticsService",
    "MAX_TRACE_EVENTS",
    "SessionSnapshot",
]
