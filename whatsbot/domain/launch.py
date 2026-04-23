"""Claude launch-command builder — pure.

Builds the argv tuple that gets sent into the tmux pane as a literal
line of text. The wrapper binary (``safe-claude`` in prod, a test stub
in integration tests) is passed in so callers can swap it for a
headless stub without touching this module.

Spec §5 (Auth-Lock) + §7 (tmux-Session pro Projekt). The flag lookup
for the permission mode lives in ``domain/modes.claude_flags`` —
we just splice it in here.
"""

from __future__ import annotations

import shlex

from whatsbot.domain.modes import claude_flags
from whatsbot.domain.projects import Mode


def build_claude_argv(
    *,
    safe_claude_binary: str,
    session_id: str,
    mode: Mode,
) -> tuple[str, ...]:
    """Return the argv tuple for launching Claude in the given mode.

    ``session_id`` may be empty — a fresh session, no ``--resume``.
    Anything non-empty becomes ``--resume <id>`` so Claude rebinds
    the existing transcript file instead of starting from scratch.
    """
    argv: list[str] = [safe_claude_binary]
    if session_id:
        argv.extend(("--resume", session_id))
    argv.extend(claude_flags(mode))
    return tuple(argv)


def render_command_line(argv: tuple[str, ...]) -> str:
    """Render an argv tuple as a shell-safe single-line command.

    The output is what the tmux-controller sends into the pane via
    ``send_text`` — the user's shell inside the pane then re-parses
    it. ``shlex.quote`` keeps names with spaces or metacharacters from
    breaking the argument boundaries.
    """
    if not argv:
        raise ValueError("argv must not be empty")
    return " ".join(shlex.quote(a) for a in argv)
