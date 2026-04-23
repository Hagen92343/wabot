"""Unit tests for the pure pieces of ``watchdog_transcript_watcher``.

Only ``read_lines_from_offset`` is tested here — it has no threading
and no FS events. The watcher-on-real-Observer smoke is in
``tests/integration/test_watchdog_transcript_watcher_real.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whatsbot.adapters.watchdog_transcript_watcher import read_lines_from_offset

pytestmark = pytest.mark.unit


def test_missing_file_returns_empty_and_same_offset(tmp_path: Path) -> None:
    target = tmp_path / "ghost.jsonl"
    lines, offset = read_lines_from_offset(target, offset=0)
    assert lines == []
    assert offset == 0


def test_empty_file_returns_empty(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    target.write_bytes(b"")
    lines, offset = read_lines_from_offset(target, offset=0)
    assert lines == []
    assert offset == 0


def test_reads_complete_lines_from_start(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    target.write_text("line-1\nline-2\nline-3\n", encoding="utf-8")
    lines, offset = read_lines_from_offset(target, offset=0)
    assert lines == ["line-1", "line-2", "line-3"]
    assert offset == len("line-1\nline-2\nline-3\n")


def test_respects_start_offset(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    target.write_text("first\nsecond\nthird\n", encoding="utf-8")
    # Skip the first line ("first\n" = 6 bytes).
    lines, offset = read_lines_from_offset(target, offset=6)
    assert lines == ["second", "third"]
    assert offset == 6 + len("second\nthird\n")


def test_trailing_partial_line_is_not_returned(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    # The last line has no trailing newline.
    target.write_text("alpha\nbeta\npartial", encoding="utf-8")
    lines, offset = read_lines_from_offset(target, offset=0)
    assert lines == ["alpha", "beta"]
    # Offset stops at just after the last complete newline — so the
    # next call picks up the partial from the start.
    assert offset == len("alpha\nbeta\n")


def test_no_complete_lines_yet(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    target.write_text("incomplete", encoding="utf-8")
    lines, offset = read_lines_from_offset(target, offset=0)
    assert lines == []
    assert offset == 0


def test_unicode_survives_broken_mid_character(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    # 'ä' = 0xC3 0xA4 in UTF-8. Truncate after the first byte of
    # a multi-byte codepoint to simulate a mid-write read.
    target.write_bytes(b"hello-\xc3\xa4ll\xc3\n")
    lines, offset = read_lines_from_offset(target, offset=0)
    # The decoder uses ``errors="replace"`` — garbled bytes become
    # U+FFFD rather than raising.
    assert len(lines) == 1
    assert "hello-äll" in lines[0]
    assert offset == len(b"hello-\xc3\xa4ll\xc3\n")


def test_second_call_continues_where_first_stopped(tmp_path: Path) -> None:
    target = tmp_path / "t.jsonl"
    target.write_text("line-1\nline-2\n", encoding="utf-8")
    _, offset_after_first = read_lines_from_offset(target, offset=0)

    with target.open("a", encoding="utf-8") as fh:
        fh.write("line-3\n")
    lines, offset = read_lines_from_offset(target, offset=offset_after_first)
    assert lines == ["line-3"]
    assert offset == offset_after_first + len("line-3\n")
