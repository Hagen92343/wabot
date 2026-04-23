"""Unit tests for FileLogReader — tail of ``app.jsonl``."""

from __future__ import annotations

import json
from pathlib import Path

from whatsbot.adapters.file_log_reader import FileLogReader


def _write(path: Path, lines: list[dict[str, object]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")


def test_read_tail_returns_parsed_entries_newest_last(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write(
        log_dir / "app.jsonl",
        [
            {"event": "a", "ts": "1"},
            {"event": "b", "ts": "2"},
            {"event": "c", "ts": "3"},
        ],
    )
    reader = FileLogReader(log_dir)

    entries = reader.read_tail(max_lines=10)

    assert [e.event for e in entries] == ["a", "b", "c"]


def test_read_tail_caps_at_max_lines(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write(
        log_dir / "app.jsonl",
        [{"event": f"e{i}", "ts": str(i)} for i in range(100)],
    )
    reader = FileLogReader(log_dir)

    entries = reader.read_tail(max_lines=5)

    # Last 5 entries preserved.
    assert [e.event for e in entries] == ["e95", "e96", "e97", "e98", "e99"]


def test_read_tail_missing_directory_returns_empty(tmp_path: Path) -> None:
    missing = tmp_path / "never-created"
    reader = FileLogReader(missing)

    assert reader.read_tail(max_lines=10) == []


def test_read_tail_missing_file_in_existing_dir(tmp_path: Path) -> None:
    (tmp_path / "logs").mkdir()
    reader = FileLogReader(tmp_path / "logs")

    assert reader.read_tail(max_lines=10) == []


def test_read_tail_zero_lines_returns_empty(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write(log_dir / "app.jsonl", [{"event": "a"}])
    reader = FileLogReader(log_dir)

    assert reader.read_tail(max_lines=0) == []
    assert reader.read_tail(max_lines=-1) == []


def test_read_tail_skips_garbage_lines_silently(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    path = log_dir / "app.jsonl"
    with path.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "good"}) + "\n")
        fh.write("this is not json\n")
        fh.write("\n")
        fh.write("[broken\n")
        fh.write(json.dumps({"event": "also-good"}) + "\n")

    reader = FileLogReader(log_dir)
    entries = reader.read_tail(max_lines=100)

    assert [e.event for e in entries] == ["good", "also-good"]


def test_read_tail_custom_filename(tmp_path: Path) -> None:
    log_dir = tmp_path / "logs"
    _write(log_dir / "audit.jsonl", [{"event": "audit-a"}])
    reader = FileLogReader(log_dir, filename="audit.jsonl")

    entries = reader.read_tail(max_lines=10)

    assert [e.event for e in entries] == ["audit-a"]
