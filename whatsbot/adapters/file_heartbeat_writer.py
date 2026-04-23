"""Filesystem-backed ``HeartbeatWriter``.

Writes to ``settings.heartbeat_path`` (default
``/tmp/whatsbot-heartbeat`` per Spec §4) atomically: write to a
sibling ``<path>.tmp``, then ``os.replace`` over the target. That
way the watchdog never reads a partial file; mtime moves only when
the new content is fully on disk.
"""

from __future__ import annotations

import contextlib
import os
from pathlib import Path

from whatsbot.logging_setup import get_logger


class FileHeartbeatWriter:
    """File-backed heartbeat writer."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._tmp_path = path.with_suffix(path.suffix + ".tmp")
        self._log = get_logger("whatsbot.heartbeat")

    def write(self, payload: str) -> None:
        # Make sure the parent dir exists — /tmp always does, but a
        # custom test path might not.
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._tmp_path.write_text(payload, encoding="utf-8")
        os.replace(self._tmp_path, self._path)

    def last_mtime(self) -> float | None:
        try:
            return self._path.stat().st_mtime
        except FileNotFoundError:
            return None

    def remove(self) -> None:
        with contextlib.suppress(FileNotFoundError):
            self._path.unlink()
