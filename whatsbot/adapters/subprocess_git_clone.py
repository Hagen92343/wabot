"""Subprocess-based GitClone adapter — shells out to the real git binary.

Decisions:
* ``--depth`` defaults to 50 (Spec §13) — keeps clones small without
  losing the recent history a code-review bot wants.
* Timeout defaults to 180s; the bot's WhatsApp command-loop relies on this
  to avoid a hung clone blocking the whole event loop.
* stderr is captured and surfaced (truncated to 500 chars) in the
  ``GitCloneError`` message so the user gets actionable feedback in
  WhatsApp — but never the raw URL with credentials, since the adapter
  doesn't echo it back.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from whatsbot.ports.git_clone import GitCloneError


class SubprocessGitClone:
    """Concrete ``GitClone`` implementation backed by ``/usr/bin/git``."""

    def __init__(self, git_binary: str = "git") -> None:
        self._git = git_binary

    def clone(
        self,
        url: str,
        dest: Path,
        *,
        depth: int = 50,
        timeout_seconds: float = 180.0,
    ) -> None:
        cmd = [
            self._git,
            "clone",
            "--depth",
            str(depth),
            "--quiet",
            url,
            str(dest),
        ]
        try:
            result = subprocess.run(  # noqa: S603 — argv list, no shell
                cmd,
                capture_output=True,
                timeout=timeout_seconds,
                check=False,
            )
        except FileNotFoundError as exc:
            raise GitCloneError(f"git binary nicht gefunden ({self._git!r}).") from exc
        except subprocess.TimeoutExpired as exc:
            raise GitCloneError(f"git clone timeout nach {timeout_seconds:.0f}s.") from exc

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace").strip()
            tail = stderr[-500:] if len(stderr) > 500 else stderr
            raise GitCloneError(f"git clone failed (exit {result.returncode}): {tail}")
