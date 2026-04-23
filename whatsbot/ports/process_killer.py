"""Process-killer port — wrapper around ``pkill`` (and friends).

Phase 6 ``/panic`` uses this as a *backstop* after every wb-* tmux
session has been killed. The intent is to catch stuck Claude
processes that didn't respond to the SIGHUP from their parent
shell. ``pkill -9 -f`` is the standard incantation.

The port exists so tests can inject a fake — we don't want the unit
tests to actually fork+exec ``pkill`` against the developer's machine.

Spec §7 + §21 Phase 6 abbruch-criterion: the pattern must be
narrow enough not to mass-kill foreign Claude installations on the
same machine. The default pattern in the wiring layer is
``"safe-claude"`` (our wrapper binary), not ``"claude"``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class ProcessKillerError(RuntimeError):
    """Raised when the ``pkill``-style call fails for an unexpected
    reason (binary missing, permissions). ``pkill`` returning 1 ("no
    matches") is *not* an error — the adapter swallows that into
    ``KillResult(matched_count=0)``."""


@dataclass(frozen=True, slots=True)
class KillResult:
    """Outcome of a kill-by-pattern call.

    ``matched_count`` is best-effort: the standard ``pkill`` doesn't
    report match counts on stdout, so the subprocess adapter returns
    ``-1`` when it cannot tell. Tests that need a precise count use
    the in-memory fake.
    """

    pattern: str
    exit_code: int
    matched_count: int


class ProcessKiller(Protocol):
    """Narrow contract for SIGKILL-by-pattern."""

    def kill_by_pattern(self, pattern: str) -> KillResult:
        """SIGKILL every process whose full cmdline matches ``pattern``.

        Pattern semantics follow ``pkill -f`` (extended regex on the
        full argv string). The caller is responsible for passing a
        narrow-enough pattern — no enclosing anchors are added.
        """
