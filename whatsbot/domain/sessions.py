"""Claude session domain model + context-fill helpers — pure.

The ``claude_sessions`` table (Spec §19) holds one row per project: the
Claude session ID (used for ``--resume``), the transcript path on disk,
running totals (turns / tokens / last compact), and the currently-
active mode. Phase 4 finally writes to and reads from this row on
every tmux-session life-cycle event.

Pure module — the SQLite adapter + SessionService consume these
structures; neither touches the disk.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Final

from whatsbot.domain.projects import Mode

# Claude's Sonnet + Opus context limit (Spec §8). We don't use this
# number directly in most call-sites — callers pass tokens_used through
# ``context_fill_ratio`` which returns a normalised 0..1.
CLAUDE_CONTEXT_LIMIT: Final[int] = 200_000

# Auto-compact fires at 80% — Spec §8 + phase-4.md C4.8. Keeping it a
# module constant instead of a magic number in the service layer.
AUTO_COMPACT_THRESHOLD: Final[float] = 0.80

# tmux session name format: ``wb-<project>`` (Spec §7). Exported as a
# function so callers don't hard-code the prefix in a dozen places.


def tmux_session_name(project_name: str) -> str:
    return f"wb-{project_name}"


@dataclass(frozen=True, slots=True)
class ClaudeSession:
    """One row of ``claude_sessions``.

    The ``session_id`` can be empty before Claude has actually started;
    we create the row on ``ensure_started`` and patch the ID in once
    the transcript file appears. ``transcript_path`` is populated the
    same way — first observed path wins and stays stable.
    """

    project_name: str
    session_id: str
    transcript_path: str
    started_at: datetime
    current_mode: Mode = Mode.NORMAL
    turns_count: int = 0
    tokens_used: int = 0
    context_fill_ratio: float = 0.0
    last_compact_at: datetime | None = None
    last_activity_at: datetime | None = None

    def with_tokens(self, tokens: int) -> ClaudeSession:
        """Return a copy with ``tokens_used`` bumped to ``tokens`` and
        ``context_fill_ratio`` recomputed. Pure."""
        return replace(
            self,
            tokens_used=tokens,
            context_fill_ratio=context_fill_ratio(tokens),
        )

    def with_mode(self, mode: Mode) -> ClaudeSession:
        return replace(self, current_mode=mode)

    def with_activity(self, ts: datetime) -> ClaudeSession:
        return replace(self, last_activity_at=ts)

    def mark_compact(self, ts: datetime) -> ClaudeSession:
        """Record a fresh /compact event. Resets tokens to 0 — Claude
        rebuilds working memory from the compact summary, so subsequent
        token counts start fresh."""
        return replace(
            self,
            last_compact_at=ts,
            tokens_used=0,
            context_fill_ratio=0.0,
        )


def context_fill_ratio(
    tokens_used: int, *, limit: int = CLAUDE_CONTEXT_LIMIT
) -> float:
    """Return the normalised fill ratio in ``[0, 1]``.

    Negative or unknown token totals clamp to ``0.0``; above the limit
    clamps to ``1.0``. The service layer uses this to decide whether
    to fire /compact — a slightly pessimistic saturation is safer
    than overshooting."""
    if limit <= 0:
        raise ValueError(f"limit must be positive, got {limit}")
    if tokens_used <= 0:
        return 0.0
    ratio = tokens_used / limit
    return min(ratio, 1.0)


def should_auto_compact(
    session: ClaudeSession, *, threshold: float = AUTO_COMPACT_THRESHOLD
) -> bool:
    """True iff the session has passed the fill threshold *and* we
    haven't already compacted at this point.

    The ``last_compact_at`` check is there to stop a stream of
    assistant turns re-triggering /compact while the ratio is still
    above threshold — once we compact, tokens_used should drop back
    to 0 via ``mark_compact``; if it hasn't, the caller should update
    its totals before re-checking.
    """
    return session.context_fill_ratio >= threshold and session.tokens_used > 0
