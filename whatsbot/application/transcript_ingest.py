"""TranscriptIngest — turn-end detection + token bookkeeping.

Phase-4 C4.2d-1. Given a stream of raw JSONL lines produced by
Claude Code, ``TranscriptIngest.feed(project, line)`` figures out:

* which events belong to the main conversation chain (Spec §7),
* when a Claude turn has ended,
* what the assistant's textual response was,
* how many tokens the conversation has consumed so far,
* whether a Max-limit event has fired.

The ingest is intentionally orchestration-only: it persists token
totals through ``ClaudeSessionRepository`` and fires a ``turn_end``
callback with the assistant's text, but it does not open the
transcript file or ship WhatsApp messages itself — those are the
responsibilities of the watcher adapter and the output pipeline,
wired in C4.2d-2 + d-3.

State is held in-memory per project. One row in
``_states[project_name]`` tracks the in-flight turn. This state
does NOT survive a bot restart; the reboot-recovery path (C4.6+)
resumes from a cold read via ``TranscriptWatcher.read_since``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from whatsbot.domain.sessions import (
    AUTO_COMPACT_THRESHOLD,
    context_fill_ratio,
)
from whatsbot.domain.transcript import (
    AssistantEvent,
    UsageLimitEvent,
    UserEvent,
    parse_line,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.claude_session_repository import ClaudeSessionRepository

# Callback fired when the ingest decides Claude has ended its turn.
# Arguments: (project_name, assistant_text).
TurnEndCallback = Callable[[str, str], None]

# Callback fired when a Max-limit event lands (Spec §14). Phase 4
# scope: surface the event to the caller; the ``max_limits`` table
# wiring lands in Phase 8.
UsageLimitCallback = Callable[[str, UsageLimitEvent], None]

# Callback fired when the context fill ratio crosses the auto-compact
# threshold (Spec §8, C4.8). Argument is the project name; the
# implementation sends ``/compact`` into the tmux pane.
AutoCompactCallback = Callable[[str], None]


@dataclass
class _IngestState:
    """Per-project in-flight turn state.

    ``assistant_text_parts`` accumulates text across an assistant
    event *sequence* — if Claude interleaves text + tool_use blocks,
    the textual pieces from every assistant event in the turn are
    concatenated so the WhatsApp payload is the full assistant
    narrative, not just the final message.

    ``last_tokens_persisted`` tracks what we last wrote to the DB
    so we don't flood the connection with identical UPDATEs on
    every line.
    """

    assistant_text_parts: list[str] = field(default_factory=list)
    max_tokens_observed: int = 0
    last_tokens_persisted: int = 0


class TranscriptIngest:
    """Stateful per-project transcript orchestrator.

    Thread-safety: each ``feed`` call takes a per-ingest lock. The
    watcher delivers lines on its own thread; serialising every
    feed keeps per-project state mutations clean even if the same
    process watches multiple projects.
    """

    def __init__(
        self,
        *,
        session_repo: ClaudeSessionRepository,
        on_turn_end: TurnEndCallback,
        on_usage_limit: UsageLimitCallback | None = None,
        on_auto_compact: AutoCompactCallback | None = None,
    ) -> None:
        self._sessions = session_repo
        self._on_turn_end = on_turn_end
        self._on_usage_limit = on_usage_limit
        self._on_auto_compact = on_auto_compact
        self._states: dict[str, _IngestState] = {}
        self._lock = threading.Lock()
        self._log = get_logger("whatsbot.ingest")

    # ---- public API ---------------------------------------------------

    def feed(self, project_name: str, line: str) -> None:
        """Handle one raw JSONL line for ``project_name``."""
        event = parse_line(line)
        if event is None:
            return

        # Sidechain + API-error events stay out of the main-chain
        # turn-end logic (Spec §7).
        if isinstance(event, UserEvent | AssistantEvent) and (
            event.is_sidechain or event.is_api_error
        ):
            return

        if isinstance(event, UserEvent):
            self._handle_user(project_name, event)
        elif isinstance(event, AssistantEvent):
            self._handle_assistant(project_name, event)
        elif isinstance(event, UsageLimitEvent):
            self._handle_usage_limit(project_name, event)
        # SystemEvent / UnknownEvent: nothing to do at C4.2d-1 scope.

    def reset(self, project_name: str) -> None:
        """Drop the in-memory state for ``project_name``.

        Called by Phase 4 on ``session_service.recycle`` (mode switch)
        so the fresh Claude process doesn't inherit half a turn from
        the previous one.
        """
        with self._lock:
            self._states.pop(project_name, None)

    # ---- event handlers -----------------------------------------------

    def _handle_user(self, project_name: str, event: UserEvent) -> None:
        if event.is_bot_prefixed:
            # Bot-originated prompt. The current turn is still the
            # bot's; we keep the assistant buffer alive so the
            # response assembles cleanly once Claude replies.
            return
        if not event.text:
            # Empty ``text`` means the user event carries only
            # tool_result blocks (the domain parser drops those
            # from the flattened text). That's Claude's own tool
            # plumbing — not a fresh human turn, no buffer reset.
            return
        # A human-typed user turn at the local terminal starts a
        # fresh conversation branch. Anything half-buffered is
        # irrelevant — the human, not WhatsApp, will see it.
        with self._lock:
            state = self._states.get(project_name)
            if state is not None:
                state.assistant_text_parts = []

    def _handle_assistant(
        self, project_name: str, event: AssistantEvent
    ) -> None:
        with self._lock:
            state = self._states.setdefault(project_name, _IngestState())
            if event.text:
                state.assistant_text_parts.append(event.text)
            total = event.usage.total
            if total > state.max_tokens_observed:
                state.max_tokens_observed = total
            tokens_to_persist = (
                total if total > state.last_tokens_persisted else None
            )
            # Detach what we need to emit under the lock, then call
            # out afterwards so callback work doesn't block further
            # feeds on this project.
            emit_text = (
                "\n".join(state.assistant_text_parts)
                if not event.has_tool_use
                else None
            )
            if not event.has_tool_use:
                state.assistant_text_parts = []

        if tokens_to_persist is not None:
            self._persist_tokens(project_name, tokens_to_persist)
        if emit_text is not None:
            self._log.info(
                "turn_end_detected",
                project=project_name,
                text_len=len(emit_text),
            )
            self._on_turn_end(project_name, emit_text)
        # Auto-compact check runs AFTER turn-end delivery so the
        # user sees the last pre-compact response before /compact
        # kicks Claude into summary mode (Spec §8 + C4.8).
        if tokens_to_persist is not None:
            self._maybe_auto_compact(project_name, tokens_to_persist)

    def _handle_usage_limit(
        self, project_name: str, event: UsageLimitEvent
    ) -> None:
        self._log.warning(
            "usage_limit_reached",
            project=project_name,
            reset_at=event.reset_at,
            limit_kind=event.limit_kind,
        )
        if self._on_usage_limit is not None:
            self._on_usage_limit(project_name, event)

    # ---- persistence --------------------------------------------------

    def _persist_tokens(self, project_name: str, tokens: int) -> None:
        """Push ``tokens`` into ``claude_sessions.tokens_used`` +
        refresh ``last_activity_at``. Only the partial-update
        path is used so ``current_mode`` / ``session_id`` aren't
        touched from the ingest thread."""
        try:
            self._sessions.update_activity(
                project_name,
                tokens_used=tokens,
                last_activity_at=datetime.now(UTC),
            )
        except Exception:  # pragma: no cover - logged, never raised
            # DB hiccups must not kill the observer thread.
            self._log.exception(
                "ingest_persist_tokens_failed", project=project_name
            )
            return
        with self._lock:
            state = self._states.get(project_name)
            if state is not None:
                state.last_tokens_persisted = tokens

    def _maybe_auto_compact(self, project_name: str, tokens: int) -> None:
        """Fire ``/compact`` when context fill crosses the threshold.

        Idempotence against the same conversation reaching 80%+ on
        multiple consecutive turns is enforced via the ``mark_compact``
        persistence step: it resets ``tokens_used`` to 0 in the DB
        AND we zero the in-memory counters too. The next assistant
        event has to re-accumulate from 0 before it can re-trigger.
        """
        if self._on_auto_compact is None:
            return
        ratio = context_fill_ratio(tokens)
        if ratio < AUTO_COMPACT_THRESHOLD:
            return

        self._log.info(
            "auto_compact_triggered",
            project=project_name,
            tokens=tokens,
            ratio=round(ratio, 3),
        )
        try:
            self._on_auto_compact(project_name)
        except Exception:  # pragma: no cover - logged
            # Callback failure (tmux down, etc.) must not kill the
            # observer thread — log and continue.
            self._log.exception(
                "auto_compact_callback_failed", project=project_name
            )
        try:
            self._sessions.mark_compact(project_name, datetime.now(UTC))
        except Exception:  # pragma: no cover - logged
            self._log.exception(
                "auto_compact_persist_failed", project=project_name
            )
        # Zero the in-memory counters so a later uptick has to climb
        # back up to threshold before re-triggering.
        with self._lock:
            state = self._states.get(project_name)
            if state is not None:
                state.max_tokens_observed = 0
                state.last_tokens_persisted = 0
