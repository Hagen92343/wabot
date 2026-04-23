"""Subprocess-backed ``ProcessKiller`` for production.

Shells out to ``pkill -9 -f <pattern>``. Exit codes map to:

* 0 → at least one process matched and was signalled (success)
* 1 → no processes matched (treated as success, ``matched_count=0``)
* 2 → invalid options (treated as ``ProcessKillerError``)
* 3 → fatal error (treated as ``ProcessKillerError``)

We don't try to *count* the matched PIDs on prod — ``pkill`` doesn't
print them. Tests that need the count run against the fake, not
this adapter.
"""

from __future__ import annotations

import shutil
import subprocess

from whatsbot.logging_setup import get_logger
from whatsbot.ports.process_killer import KillResult, ProcessKillerError


class SubprocessProcessKiller:
    """``pkill -9 -f``-shaped killer."""

    def __init__(self, *, pkill_binary: str | None = None) -> None:
        self._binary = pkill_binary or "pkill"
        self._log = get_logger("whatsbot.process_killer")
        # Pre-resolve the binary so a missing pkill surfaces at app
        # startup time, not on the panic path where you really don't
        # want any surprises.
        if shutil.which(self._binary) is None:
            self._log.warning(
                "process_killer_binary_missing",
                binary=self._binary,
            )

    def kill_by_pattern(self, pattern: str) -> KillResult:
        if not pattern.strip():
            raise ProcessKillerError("empty pattern is not allowed")
        try:
            completed = subprocess.run(  # noqa: S603 — argv list, no shell
                [self._binary, "-9", "-f", pattern],
                capture_output=True,
                check=False,
                timeout=10.0,
            )
        except FileNotFoundError as exc:
            raise ProcessKillerError(
                f"{self._binary!r} not found on PATH"
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise ProcessKillerError(
                f"{self._binary!r} timed out after 10 s"
            ) from exc
        if completed.returncode in (2, 3):
            raise ProcessKillerError(
                f"{self._binary!r} failed (exit {completed.returncode}): "
                f"{completed.stderr.decode(errors='replace').strip()}"
            )
        # 0 = matched, 1 = none-matched. Either is success.
        self._log.info(
            "process_killer_executed",
            pattern=pattern,
            exit_code=completed.returncode,
        )
        return KillResult(
            pattern=pattern,
            exit_code=completed.returncode,
            matched_count=-1,  # subprocess can't tell
        )
