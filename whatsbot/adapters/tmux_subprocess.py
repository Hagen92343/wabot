"""Subprocess-backed TmuxController.

Every method maps to exactly one ``tmux`` invocation. ``shell=False``
always — arguments go as a list so user-supplied names/text can't be
re-interpreted by a shell. The ``-l`` (literal) flag for ``send-keys``
plus a follow-up ``Enter`` keypress keeps prompts intact regardless of
what's inside them (quotes, dollar signs, semicolons).

On non-zero exit we raise ``TmuxError`` with a short diagnostic
including stderr — except for ``has_session``, which is defined to
communicate existence through the exit code and returns a bool
instead.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from whatsbot.logging_setup import get_logger
from whatsbot.ports.tmux_controller import TmuxError


class SubprocessTmuxController:
    """Concrete TmuxController driving the local ``tmux`` binary."""

    def __init__(self, *, tmux_binary: str = "tmux") -> None:
        self._tmux = tmux_binary
        self._log = get_logger("whatsbot.tmux")

    # ---- queries -------------------------------------------------------

    def has_session(self, name: str) -> bool:
        # ``tmux has-session`` exits 0 iff the session exists, 1 otherwise.
        # Any other error (tmux missing, server unreachable) bubbles via
        # FileNotFoundError from subprocess.run.
        completed = self._run(["has-session", "-t", name], check=False)
        return completed.returncode == 0

    def list_sessions(self, *, prefix: str | None = None) -> list[str]:
        # ``tmux ls`` exits 1 when there's no server running — treat
        # that as "no sessions" rather than an error.
        completed = self._run(
            ["list-sessions", "-F", "#{session_name}"], check=False
        )
        if completed.returncode != 0:
            return []
        names = [ln for ln in completed.stdout.splitlines() if ln]
        if prefix is not None:
            names = [n for n in names if n.startswith(prefix)]
        return names

    # ---- lifecycle -----------------------------------------------------

    def new_session(self, name: str, *, cwd: Path | str) -> None:
        cwd_str = str(cwd)
        completed = self._run(
            ["new-session", "-d", "-s", name, "-c", cwd_str], check=False
        )
        if completed.returncode != 0:
            raise TmuxError(
                f"new-session failed for {name!r}: {completed.stderr.strip()}"
            )
        self._log.info("tmux_session_created", name=name, cwd=cwd_str)

    def kill_session(self, name: str) -> bool:
        # ``tmux kill-session`` exits 1 if the session doesn't exist. We
        # collapse that to ``return False`` rather than raising so the
        # caller can use this as a "best-effort cleanup".
        completed = self._run(
            ["kill-session", "-t", name], check=False
        )
        if completed.returncode == 0:
            self._log.info("tmux_session_killed", name=name)
            return True
        return False

    # ---- I/O -----------------------------------------------------------

    def send_text(self, name: str, text: str) -> None:
        # Two-step: literal text, then Enter. Splitting the two keeps
        # the literal flag scoped to the string the caller wants
        # echoed, without tmux re-interpreting "Enter" as a 5-char literal.
        literal = self._run(
            ["send-keys", "-l", "-t", name, "--", text], check=False
        )
        if literal.returncode != 0:
            raise TmuxError(
                f"send-keys -l failed for {name!r}: {literal.stderr.strip()}"
            )
        enter = self._run(
            ["send-keys", "-t", name, "Enter"], check=False
        )
        if enter.returncode != 0:
            raise TmuxError(
                f"send-keys Enter failed for {name!r}: {enter.stderr.strip()}"
            )

    def interrupt(self, name: str) -> None:
        # Single send-keys with the tmux key event ``C-c`` (no -l, no
        # Enter). tmux translates that into Ctrl+C on the foreground
        # process, which Claude Code receives as SIGINT.
        completed = self._run(
            ["send-keys", "-t", name, "C-c"], check=False
        )
        if completed.returncode != 0:
            raise TmuxError(
                f"send-keys C-c failed for {name!r}: "
                f"{completed.stderr.strip()}"
            )
        self._log.info("tmux_interrupt_sent", name=name)

    # ---- theming -------------------------------------------------------

    def set_status(self, name: str, *, color: str, label: str) -> None:
        # Two set-option calls. ``-g`` would apply globally; we scope
        # to the specific session with ``-t <name>`` so each project
        # keeps its own bar theme.
        style = self._run(
            [
                "set-option",
                "-t",
                name,
                "status-style",
                f"bg={color},fg=white",
            ],
            check=False,
        )
        if style.returncode != 0:
            raise TmuxError(
                f"set-option status-style failed for {name!r}: "
                f"{style.stderr.strip()}"
            )
        right = self._run(
            ["set-option", "-t", name, "status-right", label], check=False
        )
        if right.returncode != 0:
            raise TmuxError(
                f"set-option status-right failed for {name!r}: "
                f"{right.stderr.strip()}"
            )
        self._log.info(
            "tmux_status_set", name=name, color=color, label=label
        )

    # ---- internals -----------------------------------------------------

    def _run(
        self, args: list[str], *, check: bool
    ) -> subprocess.CompletedProcess[str]:
        """Run ``tmux <args>`` and return the CompletedProcess.

        Never uses ``shell=True``; arguments travel as a list. Text
        mode because every tmux output is ASCII / UTF-8. Inputs are
        internal state (project names validated on /new, subcommand
        literals from this module) — no user-typed strings land here
        unescaped.
        """
        return subprocess.run(  # noqa: S603 — shell=False, vetted args
            [self._tmux, *args],
            capture_output=True,
            text=True,
            check=check,
        )
