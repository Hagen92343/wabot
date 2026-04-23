"""Unit tests for ``whatsbot.adapters.file_heartbeat_writer``."""

from __future__ import annotations

from pathlib import Path

import pytest

from whatsbot.adapters.file_heartbeat_writer import FileHeartbeatWriter

pytestmark = pytest.mark.unit


def test_write_creates_file_with_payload(tmp_path: Path) -> None:
    target = tmp_path / "hb"
    FileHeartbeatWriter(target).write("hello")
    assert target.read_text(encoding="utf-8") == "hello"


def test_write_overwrites(tmp_path: Path) -> None:
    target = tmp_path / "hb"
    w = FileHeartbeatWriter(target)
    w.write("first")
    w.write("second")
    assert target.read_text(encoding="utf-8") == "second"


def test_write_creates_parent_dir(tmp_path: Path) -> None:
    """Custom test path can have a missing parent — the writer must
    still produce the file."""
    target = tmp_path / "deeper" / "still-deeper" / "hb"
    FileHeartbeatWriter(target).write("payload")
    assert target.read_text(encoding="utf-8") == "payload"


def test_write_is_atomic_via_replace(tmp_path: Path) -> None:
    """We can't easily prove atomicity in a unit test, but we can
    prove the .tmp sibling is gone after a successful write — that
    is the observable trace of the ``os.replace`` step."""
    target = tmp_path / "hb"
    FileHeartbeatWriter(target).write("done")
    siblings = sorted(p.name for p in tmp_path.iterdir())
    assert siblings == ["hb"]


def test_last_mtime_returns_none_when_missing(tmp_path: Path) -> None:
    assert FileHeartbeatWriter(tmp_path / "nope").last_mtime() is None


def test_last_mtime_returns_float_after_write(tmp_path: Path) -> None:
    target = tmp_path / "hb"
    w = FileHeartbeatWriter(target)
    w.write("x")
    mt = w.last_mtime()
    assert isinstance(mt, float)
    assert mt > 0


def test_remove_unlinks_file(tmp_path: Path) -> None:
    target = tmp_path / "hb"
    w = FileHeartbeatWriter(target)
    w.write("x")
    w.remove()
    assert not target.exists()


def test_remove_is_idempotent_when_missing(tmp_path: Path) -> None:
    """Should not raise when the file isn't there — graceful shutdown
    might run twice."""
    FileHeartbeatWriter(tmp_path / "nope").remove()  # must not raise
