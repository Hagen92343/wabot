"""Unit tests for whatsbot.domain.claude_paths."""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from whatsbot.domain.claude_paths import (
    claude_projects_dir,
    encode_cwd,
    expected_transcript_path,
    extract_session_id,
    find_latest_transcript_since,
)

pytestmark = pytest.mark.unit


# ---- encode_cwd -----------------------------------------------------


def test_encode_cwd_replaces_slashes_with_hyphens() -> None:
    assert encode_cwd(Path("/Users/foo/bar")) == "-Users-foo-bar"


def test_encode_cwd_keeps_spaces_literal() -> None:
    # Real transcripts (see ~/.claude/projects/ inventory) preserve
    # spaces rather than percent-encoding them.
    assert (
        encode_cwd(Path("/Users/foo/CV ersteller"))
        == "-Users-foo-CV ersteller"
    )


def test_encode_cwd_resolves_relative(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    encoded = encode_cwd(Path("."))
    assert encoded.startswith("-")
    # The encoded path corresponds to the resolved tmp_path.
    assert encoded == str(tmp_path.resolve()).replace("/", "-")


# ---- claude_projects_dir -------------------------------------------


def test_claude_projects_dir_composes_with_claude_home(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    cwd = Path("/Users/foo/bar")
    result = claude_projects_dir(cwd, claude_home=claude_home)
    assert result == claude_home / "projects" / "-Users-foo-bar"


# ---- expected_transcript_path --------------------------------------


def test_expected_transcript_path_uses_session_id_as_filename(tmp_path: Path) -> None:
    claude_home = tmp_path / "claude"
    path = expected_transcript_path(
        Path("/Users/foo/bar"),
        "abc-123",
        claude_home=claude_home,
    )
    assert path == claude_home / "projects" / "-Users-foo-bar" / "abc-123.jsonl"


def test_expected_transcript_path_requires_session_id() -> None:
    with pytest.raises(ValueError):
        expected_transcript_path(Path("/tmp/whatever"), "")


# ---- find_latest_transcript_since ----------------------------------


def test_find_latest_transcript_since_empty_dir(tmp_path: Path) -> None:
    assert find_latest_transcript_since(tmp_path) is None


def test_find_latest_transcript_since_missing_dir(tmp_path: Path) -> None:
    assert find_latest_transcript_since(tmp_path / "does-not-exist") is None


def test_find_latest_transcript_picks_newest_jsonl(tmp_path: Path) -> None:
    older = tmp_path / "older.jsonl"
    newer = tmp_path / "newer.jsonl"
    older.write_text("{}\n")
    newer.write_text("{}\n")
    # Force distinct mtimes — some filesystems round.
    os.utime(older, (time.time() - 10, time.time() - 10))
    assert find_latest_transcript_since(tmp_path) == newer


def test_find_latest_transcript_respects_since_mtime(tmp_path: Path) -> None:
    stale = tmp_path / "stale.jsonl"
    fresh = tmp_path / "fresh.jsonl"
    stale.write_text("{}\n")
    fresh.write_text("{}\n")
    now = time.time()
    os.utime(stale, (now - 100, now - 100))
    # Barrier sits between the two timestamps.
    assert find_latest_transcript_since(tmp_path, since_mtime=now - 50) == fresh


def test_find_latest_transcript_skips_non_jsonl(tmp_path: Path) -> None:
    (tmp_path / "not-a-transcript.log").write_text("stuff")
    (tmp_path / "real.jsonl").write_text("{}\n")
    assert find_latest_transcript_since(tmp_path) == tmp_path / "real.jsonl"


def test_find_latest_transcript_skips_subdirs(tmp_path: Path) -> None:
    (tmp_path / "subdir.jsonl").mkdir()  # pathological: dir with jsonl suffix
    (tmp_path / "real.jsonl").write_text("{}\n")
    assert find_latest_transcript_since(tmp_path) == tmp_path / "real.jsonl"


# ---- extract_session_id --------------------------------------------


def test_extract_session_id_returns_filename_stem() -> None:
    p = Path("/a/b/c/abc-123.jsonl")
    assert extract_session_id(p) == "abc-123"
