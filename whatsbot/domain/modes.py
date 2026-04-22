"""Mode-state-transitions + Claude-Code CLI flag lookups.

The ``Mode`` enum itself lives in ``domain/projects.py`` (Phase 2); this
module adds the *behaviour* around it — the pure bits that C4.1+ use
to decide what flag to hand the ``safe-claude`` wrapper and what colour
to paint the tmux status bar.

All pure. No subprocess, no I/O. The adapters + SessionService consume
these results to build real command lines and tmux theme commands.
"""

from __future__ import annotations

from typing import Final

from whatsbot.domain.projects import Mode

# Claude Code CLI flags per mode. Spec §6:
#   Normal → default permission mode (no extra flag)
#   Strict → --permission-mode dontAsk  (auto-deny unknown)
#   YOLO   → --dangerously-skip-permissions
_CLAUDE_FLAGS: Final[dict[Mode, tuple[str, ...]]] = {
    Mode.NORMAL: (),
    Mode.STRICT: ("--permission-mode", "dontAsk"),
    Mode.YOLO: ("--dangerously-skip-permissions",),
}

# tmux status-bar colours per Spec §6 (green / blue / red).
_STATUS_COLOR: Final[dict[Mode, str]] = {
    Mode.NORMAL: "green",
    Mode.STRICT: "blue",
    Mode.YOLO: "red",
}

# Emoji badge used in the status line + WhatsApp footer (Spec §6).
_MODE_BADGE: Final[dict[Mode, str]] = {
    Mode.NORMAL: "🟢 NORMAL",
    Mode.STRICT: "🔵 STRICT",
    Mode.YOLO: "🔴 YOLO",
}


def claude_flags(mode: Mode) -> tuple[str, ...]:
    """Return the CLI flag tuple for ``safe-claude`` in ``mode``.

    Empty tuple for Normal (no extra flag — Claude defaults). Returned
    as an immutable tuple so adapters can ``*claude_flags(mode)`` into
    an ``argv`` list without copy defensiveness.
    """
    return _CLAUDE_FLAGS[mode]


def status_bar_color(mode: Mode) -> str:
    """Return the tmux status-bar colour name for ``mode`` (green/blue/red)."""
    return _STATUS_COLOR[mode]


def mode_badge(mode: Mode) -> str:
    """Return the short emoji badge shown in the tmux status line."""
    return _MODE_BADGE[mode]


def valid_transition(from_mode: Mode, to_mode: Mode) -> bool:
    """All six directed transitions are currently allowed.

    The function exists so later, direction-dependent rules (e.g. "YOLO
    → Strict must go via Normal so the user re-reads the CLAUDE.md
    instructions") have a single place to live. Today it's a constant
    ``True`` — but by calling it from the service layer we avoid
    scattering policy once it grows.
    """
    # Same-mode is a no-op but valid (idempotent /mode calls).
    del from_mode, to_mode
    return True
