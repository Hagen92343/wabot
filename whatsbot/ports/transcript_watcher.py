"""TranscriptWatcher port — abstraction over file-tailing.

Claude Code appends JSONL lines to
``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl``. Phase 4
needs to react to each new line as it lands so the redaction +
WhatsApp-send pipeline can fire as soon as Claude finishes a turn.

The port intentionally works in "tail from now" mode: `watch()`
returns a handle and delivers every line appended *after* the
handle was created. For cold reads (reboot recovery, backfill)
use ``read_since``.

Implementations must deliver callbacks strictly in file order. They
are free to fire them from any thread — the caller is responsible
for thread-safe handoff (the ingest service uses a thread-safe
queue).

Phase-4 scope. ``WatchdogTranscriptWatcher`` is the concrete
adapter; tests ship a fake that lets them pump arbitrary lines
through the callback synchronously.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WatchHandle:
    """Opaque token returned by ``watch``. ``id`` is process-unique so
    two concurrent watches can be disambiguated; ``path`` is carried
    for diagnostics only."""

    id: str
    path: Path = field(compare=False)


LineCallback = Callable[[str], None]


class TranscriptWatcherError(RuntimeError):
    """Raised when the watcher itself can't continue (observer thread
    dead, underlying file gone). Delivery of a single malformed line
    is *not* an error — that's what ``domain.transcript.parse_line``
    tolerates via ``None`` returns."""


class TranscriptWatcher(Protocol):
    """Tail-one-file-forever abstraction."""

    def watch(
        self,
        path: Path,
        callback: LineCallback,
        *,
        from_offset: int = 0,
    ) -> WatchHandle:
        """Start watching ``path``.

        ``from_offset`` is the byte position after which new content
        should be surfaced; callers that opened the file previously
        pass in the previously-observed file size to resume exactly
        where they left off. The default (``0``) means "everything
        currently on disk + everything appended later".

        The callback receives one complete line per invocation — the
        adapter handles partial-line buffering. Trailing newlines are
        stripped.

        If ``path`` does not exist yet, the watcher must NOT raise —
        Claude Code creates the transcript lazily. The adapter
        watches the parent directory and starts emitting lines once
        the file appears.
        """

    def unwatch(self, handle: WatchHandle) -> None:
        """Stop the watcher behind ``handle`` and release all resources.

        Idempotent — calling twice is a no-op on the second call.
        After this returns, no further callbacks fire from this
        handle. Implementations should join observer threads so
        there's no racy trailing delivery.
        """

    def read_since(
        self, path: Path, offset: int
    ) -> tuple[list[str], int]:
        """Cold read.

        Returns every complete line between ``offset`` and the
        current end-of-file, along with the new offset (for
        subsequent ``read_since`` or ``watch(from_offset=...)``
        calls). Missing files yield ``([], offset)``; a trailing
        partial line (no newline at EOF) is left in place and
        re-read next time.
        """
