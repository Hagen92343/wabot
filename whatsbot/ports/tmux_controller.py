"""TmuxController port — abstraction over tmux session operations.

The bot manages one tmux session per project (Spec §7: name format
``wb-<project>``). Claude Code runs inside that session as a child
process, and the bot drives it via ``send_text``. This Protocol is
intentionally narrow — each method maps to a single ``tmux`` subcommand
so the concrete adapter stays audit-friendly.

Phase 4 uses the methods below. ``capture_pane`` (for the Phase-8
max-limit status-line parser) and ``send_control`` (for Phase-6's
``/stop`` Ctrl+C path) will be added when those phases land — not now.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class TmuxError(RuntimeError):
    """Raised when an unexpected ``tmux`` invocation fails.

    ``has_session`` returns a bool (exit-code-driven) and is *not* in
    the raising path; every other method raises this on non-zero exit.
    """


class TmuxController(Protocol):
    """Narrow, subcommand-aligned tmux controller."""

    def has_session(self, name: str) -> bool:
        """True iff a tmux session with ``name`` exists."""

    def new_session(self, name: str, *, cwd: Path | str) -> None:
        """Create a detached tmux session. Raises if one already exists."""

    def send_text(self, name: str, text: str) -> None:
        """Send ``text`` + Enter to the session's current pane.

        The adapter must pass ``text`` as a literal — tmux can't
        interpret ``$VARS``, ``~`` expansions, or backslash escapes
        beyond what the shell running inside the pane does on its own.
        """

    def kill_session(self, name: str) -> bool:
        """Destroy the session. Returns ``True`` iff one was killed."""

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        """Return session names, optionally filtered to those starting
        with ``prefix`` (e.g. ``"wb-"`` to see only bot-owned ones)."""

    def set_status(self, name: str, *, color: str, label: str) -> None:
        """Re-theme the session's status bar. ``color`` is a tmux name
        (``green``/``blue``/``red``); ``label`` is the right-aligned
        text (e.g. ``"🟢 NORMAL [wb-alpha]"``)."""
