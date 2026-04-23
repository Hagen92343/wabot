"""Watchdog-backed TranscriptWatcher.

One ``watchdog.observers.Observer`` per watch. We watch the *parent
directory* rather than the file itself so the lazy-create race
(transcript file comes into existence after watch() returns) is
covered — we just filter events to the target path.

Thread model: watchdog runs the FS-event dispatcher on a background
thread and calls our handler there. The handler reads from the file
from the last known offset, splits on newlines, buffers any partial
trailing line, and invokes the user callback once per complete line.
``unwatch`` stops + joins the observer so no trailing callback can
fire after the caller thinks the watch is gone.

``read_since`` is a bare subprocess-free file read: open, seek,
read, split. It can run on any thread and doesn't need the observer.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer
from watchdog.observers.api import BaseObserver

from whatsbot.logging_setup import get_logger
from whatsbot.ports.transcript_watcher import (
    LineCallback,
    WatchHandle,
)


@dataclass
class _WatchContext:
    observer: BaseObserver
    handler: _TailHandler


class WatchdogTranscriptWatcher:
    """Concrete TranscriptWatcher driving ``watchdog.observers.Observer``."""

    def __init__(self) -> None:
        self._contexts: dict[str, _WatchContext] = {}
        self._contexts_lock = threading.Lock()
        self._log = get_logger("whatsbot.transcript")

    # ---- public API ---------------------------------------------------

    def watch(
        self,
        path: Path,
        callback: LineCallback,
        *,
        from_offset: int = 0,
    ) -> WatchHandle:
        resolved = path.resolve()
        handler = _TailHandler(
            target_path=resolved,
            offset=from_offset,
            callback=callback,
        )
        observer: BaseObserver = Observer()
        parent = resolved.parent
        parent.mkdir(parents=True, exist_ok=True)
        # Watch the parent directory (non-recursive) — covers both
        # "file exists already" and "file will be created later".
        observer.schedule(handler, str(parent), recursive=False)
        observer.start()

        handle_id = uuid.uuid4().hex
        ctx = _WatchContext(observer=observer, handler=handler)
        with self._contexts_lock:
            self._contexts[handle_id] = ctx
        # If the file already has content beyond from_offset, drain
        # it immediately so callers don't have to wait for the next
        # filesystem event to fire a catch-up read.
        handler.drain()

        self._log.info(
            "transcript_watch_started",
            path=str(resolved),
            from_offset=from_offset,
            handle_id=handle_id,
        )
        return WatchHandle(id=handle_id, path=resolved)

    def unwatch(self, handle: WatchHandle) -> None:
        with self._contexts_lock:
            ctx = self._contexts.pop(handle.id, None)
        if ctx is None:
            # Idempotent: second unwatch on the same handle is a no-op.
            return
        ctx.observer.stop()
        # Join with a generous timeout — the observer thread exits
        # almost immediately once stopped, but we don't want to
        # hang shutdown if something went wrong.
        ctx.observer.join(timeout=2.0)
        self._log.info(
            "transcript_watch_stopped",
            path=str(handle.path),
            handle_id=handle.id,
        )

    def read_since(
        self, path: Path, offset: int
    ) -> tuple[list[str], int]:
        return read_lines_from_offset(path, offset)


class _TailHandler(FileSystemEventHandler):
    """Poll-on-event file tailer.

    State (offset + buffer) lives entirely inside the handler and is
    mutated only under ``_lock``: both the observer thread and the
    manual ``drain`` caller serialise through it so callbacks fire
    strictly in file order.
    """

    def __init__(
        self,
        *,
        target_path: Path,
        offset: int,
        callback: LineCallback,
    ) -> None:
        super().__init__()
        self._target = target_path
        self._offset = offset
        self._buffer = ""
        self._callback = callback
        self._lock = threading.Lock()
        self._log = get_logger("whatsbot.transcript")

    # FileSystemEventHandler API -----------------------------------

    def on_created(self, event: FileSystemEvent) -> None:
        self._maybe_drain(event)

    def on_modified(self, event: FileSystemEvent) -> None:
        self._maybe_drain(event)

    def on_moved(self, event: FileSystemEvent) -> None:
        # A file-moved-into-place is equivalent to "now present".
        dest_path = getattr(event, "dest_path", None)
        if isinstance(dest_path, str) and Path(dest_path) == self._target:
            self.drain()

    # Manual drain ------------------------------------------------

    def drain(self) -> None:
        """Re-read from the last offset to EOF and deliver complete
        lines to the callback. Partial trailing bytes stay in the
        handler's buffer until the next drain."""
        to_deliver: list[str] = []
        with self._lock:
            if not self._target.exists():
                return
            try:
                with self._target.open("rb") as fh:
                    fh.seek(self._offset)
                    chunk = fh.read()
            except OSError:
                return
            if chunk:
                self._offset += len(chunk)
                combined = self._buffer + chunk.decode("utf-8", errors="replace")
                parts = combined.split("\n")
                # Last piece has no trailing \n → it's either empty
                # (chunk ended cleanly) or a partial line.
                self._buffer = parts.pop()
                to_deliver = parts
            callback = self._callback
        for line in to_deliver:
            try:
                callback(line)
            except Exception:  # pragma: no cover - logged, never raised
                self._log.exception(
                    "transcript_callback_failed", path=str(self._target)
                )

    # Internals ----------------------------------------------------

    def _maybe_drain(self, event: FileSystemEvent) -> None:
        src = getattr(event, "src_path", None)
        if isinstance(src, str) and Path(src) == self._target:
            self.drain()


# ---- file I/O helpers ------------------------------------------------


def read_lines_from_offset(
    path: Path, offset: int
) -> tuple[list[str], int]:
    """Cold read: open ``path`` (if present), return every complete
    line from ``offset`` to the last newline, plus the byte offset
    one past the last newline (i.e. the start of any partial trailing
    line, which the caller will see again on the next call).

    Missing file or seek/read failure yields ``([], offset)`` so
    callers can retry cleanly.
    """
    if not path.exists():
        return ([], offset)
    try:
        with path.open("rb") as fh:
            fh.seek(offset)
            chunk = fh.read()
    except OSError:
        return ([], offset)

    if not chunk:
        return ([], offset)

    # Find the position of the last newline in the chunk. Bytes
    # beyond that are the partial trailing line; we leave them for
    # the next cold read.
    last_nl = chunk.rfind(b"\n")
    if last_nl < 0:
        # No complete lines yet.
        return ([], offset)
    complete_bytes = chunk[: last_nl + 1]
    text = complete_bytes.decode("utf-8", errors="replace")
    # Drop the trailing empty split caused by the final \n.
    lines = text.split("\n")[:-1]
    return (lines, offset + len(complete_bytes))
