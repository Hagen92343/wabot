"""Integration smoke for ``WatchdogTranscriptWatcher`` against a real
``watchdog.observers.Observer``.

Each test uses a fresh temp dir + watcher + unwatch in teardown so
stray observer threads can't leak between tests. Callbacks run on
the observer thread, so we collect lines in a thread-safe buffer
and poll for expected counts with a short timeout.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from whatsbot.adapters.watchdog_transcript_watcher import (
    WatchdogTranscriptWatcher,
)

pytestmark = pytest.mark.integration

# Generous timeout for filesystem events on macOS — FSEvents can
# coalesce within ~200ms, so we give each assertion up to 3s.
_POLL_TIMEOUT_SECONDS = 3.0
_POLL_INTERVAL_SECONDS = 0.05


class _Collector:
    """Thread-safe line collector for the callback."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._lines: list[str] = []

    def append(self, line: str) -> None:
        with self._lock:
            self._lines.append(line)

    def snapshot(self) -> list[str]:
        with self._lock:
            return list(self._lines)


def _wait_for_count(collector: _Collector, expected: int) -> list[str]:
    """Block up to ``_POLL_TIMEOUT_SECONDS`` until the collector has
    at least ``expected`` lines. Returns the final snapshot (may be
    longer than ``expected`` if more lines arrived)."""
    deadline = time.monotonic() + _POLL_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        snap = collector.snapshot()
        if len(snap) >= expected:
            return snap
        time.sleep(_POLL_INTERVAL_SECONDS)
    return collector.snapshot()


@pytest.fixture
def watcher() -> Iterator[WatchdogTranscriptWatcher]:
    w = WatchdogTranscriptWatcher()
    yield w
    # Any watch left over (e.g. a test that failed before unwatch)
    # should be cleaned up by tearing down the observer — but we
    # have no public accessor, so tests that keep a handle around
    # must call unwatch themselves. If they didn't, that's a leak
    # worth catching (test will time out on observer join next run).


def test_appends_after_watch_are_delivered(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("", encoding="utf-8")  # start empty
    collector = _Collector()

    handle = watcher.watch(target, collector.append)
    try:
        with target.open("a", encoding="utf-8") as fh:
            fh.write("alpha\n")
            fh.flush()
        with target.open("a", encoding="utf-8") as fh:
            fh.write("beta\n")
            fh.flush()
        lines = _wait_for_count(collector, 2)
        assert lines == ["alpha", "beta"]
    finally:
        watcher.unwatch(handle)


def test_existing_content_past_offset_is_delivered_on_watch(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("preexisting-1\npreexisting-2\n", encoding="utf-8")
    collector = _Collector()

    handle = watcher.watch(target, collector.append, from_offset=0)
    try:
        lines = _wait_for_count(collector, 2)
        assert lines[:2] == ["preexisting-1", "preexisting-2"]
    finally:
        watcher.unwatch(handle)


def test_from_offset_skips_already_seen_bytes(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("already-seen\nnew-1\n", encoding="utf-8")
    collector = _Collector()

    # Start after the first line (already-seen\n = 13 bytes).
    handle = watcher.watch(
        target, collector.append, from_offset=len("already-seen\n")
    )
    try:
        lines = _wait_for_count(collector, 1)
        assert lines == ["new-1"]
    finally:
        watcher.unwatch(handle)


def test_file_created_after_watch_starts_is_picked_up(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "late.jsonl"  # does NOT exist yet
    collector = _Collector()

    handle = watcher.watch(target, collector.append)
    try:
        # Delay the write so the watch() call returns first. In
        # practice Claude Code creates the transcript file only
        # once the first event is written.
        time.sleep(0.05)
        target.write_text("first-line\n", encoding="utf-8")
        lines = _wait_for_count(collector, 1)
        assert lines == ["first-line"]
    finally:
        watcher.unwatch(handle)


def test_partial_line_is_delivered_after_completion(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "partial.jsonl"
    target.write_text("", encoding="utf-8")
    collector = _Collector()

    handle = watcher.watch(target, collector.append)
    try:
        with target.open("a", encoding="utf-8") as fh:
            fh.write("no-newline-yet")
            fh.flush()
        # Without the trailing newline, the collector must still be
        # empty — partial lines are buffered.
        time.sleep(0.2)
        assert collector.snapshot() == []

        # Complete the line.
        with target.open("a", encoding="utf-8") as fh:
            fh.write("-and-then-done\n")
            fh.flush()
        lines = _wait_for_count(collector, 1)
        assert lines == ["no-newline-yet-and-then-done"]
    finally:
        watcher.unwatch(handle)


def test_unwatch_stops_future_callbacks(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("", encoding="utf-8")
    collector = _Collector()
    handle = watcher.watch(target, collector.append)

    with target.open("a", encoding="utf-8") as fh:
        fh.write("before-unwatch\n")
        fh.flush()
    _wait_for_count(collector, 1)

    watcher.unwatch(handle)

    with target.open("a", encoding="utf-8") as fh:
        fh.write("after-unwatch\n")
        fh.flush()
    # Give the filesystem a chance to fire the event.
    time.sleep(0.3)
    assert collector.snapshot() == ["before-unwatch"]


def test_unwatch_is_idempotent(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("", encoding="utf-8")
    handle = watcher.watch(target, lambda _line: None)
    watcher.unwatch(handle)
    # Second call must not raise.
    watcher.unwatch(handle)


def test_read_since_public_api(
    watcher: WatchdogTranscriptWatcher, tmp_path: Path
) -> None:
    target = tmp_path / "session.jsonl"
    target.write_text("one\ntwo\n", encoding="utf-8")
    lines, offset = watcher.read_since(target, offset=0)
    assert lines == ["one", "two"]
    assert offset == len("one\ntwo\n")
