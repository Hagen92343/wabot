"""FileLogReader — tails ``app.jsonl`` from ``settings.log_dir``.

Bounded-memory: we read the file end, keep the last ``max_lines``
lines via :class:`collections.deque`, parse each into a
:class:`LogEntry`. A missing log directory or an empty file yields
an empty list — that's the legitimate state right after a
``make reset-db`` and shouldn't crash ``/log``.

The reader is intentionally dumb about rotation: ``app.jsonl`` is
the current file, older backups (``app.jsonl.1``, ``.2``, …) are
not scanned. If a trace the user asks about predates rotation it's
gone, and that's fine - Spec §15 caps at 10 MB x 5, which is a lot
of message traces.
"""

from __future__ import annotations

from collections import deque
from pathlib import Path

from whatsbot.domain.log_events import LogEntry, parse_log_line


class FileLogReader:
    """Bounded tail of the app-log JSONL file."""

    def __init__(self, log_dir: Path, *, filename: str = "app.jsonl") -> None:
        self._log_dir = log_dir
        self._filename = filename

    def read_tail(self, *, max_lines: int) -> list[LogEntry]:
        if max_lines <= 0:
            return []

        path = self._log_dir / self._filename
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                tail: deque[str] = deque(fh, maxlen=max_lines)
        except FileNotFoundError:
            return []
        except OSError:
            # Permissions / disk error — surface as "no entries"
            # rather than crashing the command handler.
            return []

        entries: list[LogEntry] = []
        for line in tail:
            parsed = parse_log_line(line)
            if parsed is not None:
                entries.append(parsed)
        return entries
