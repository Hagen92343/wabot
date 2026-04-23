"""TmuxController port — abstraction over tmux session operations.

The bot manages one tmux session per project (Spec §7: name format
``wb-<project>``). Claude Code runs inside that session as a child
process, and the bot drives it via ``send_text``. This Protocol is
intentionally narrow — each method maps to a single ``tmux`` subcommand
so the concrete adapter stays audit-friendly.

Phase 4 uses the basic methods. Phase 6 adds ``interrupt`` for the
``/stop`` Ctrl+C path. ``capture_pane`` (Phase 8 max-limit
status-line parser) lands later.
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

    def interrupt(self, name: str) -> None:
        """Send Ctrl+C to the session's current pane (Phase 6 ``/stop``).

        Different from ``send_text``: no Enter, no literal-mode — the
        adapter must pass ``C-c`` as a tmux key event so the running
        Claude process receives SIGINT, not a 3-character literal.
        """

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        """Return session names, optionally filtered to those starting
        with ``prefix`` (e.g. ``"wb-"`` to see only bot-owned ones)."""

    def set_status(self, name: str, *, color: str, label: str) -> None:
        """Re-theme the session's status bar. ``color`` is a tmux name
        (``green``/``blue``/``red``); ``label`` is the right-aligned
        text (e.g. ``"🟢 NORMAL [wb-alpha]"``)."""
