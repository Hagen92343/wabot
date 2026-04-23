"""LogReader port — reads tail of the bot's JSONL log sinks.

Phase 8 C8.2 diagnostics commands (``/log``, ``/errors``) consume
a ``LogReader`` to surface recent events from
``settings.log_dir``. The adapter lives in
:mod:`whatsbot.adapters.file_log_reader`; tests inject fakes
in-memory.

The port keeps I/O off the :class:`DiagnosticsService` path so the
service itself stays easy to unit-test without tmp-path fixtures.
"""

from __future__ import annotations

from typing import Protocol

from whatsbot.domain.log_events import LogEntry


class LogReader(Protocol):
    """Tail of the bot's JSONL log sinks."""

    def read_tail(self, *, max_lines: int) -> list[LogEntry]:
        """Return up to ``max_lines`` parsed entries, newest last.

        Non-JSON lines and malformed events are silently skipped —
        see :func:`whatsbot.domain.log_events.parse_log_line`.
        An empty log directory / missing file yields ``[]``.
        """
