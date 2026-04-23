"""C7.4 — domain/transcription.py unit tests."""

from __future__ import annotations

import pytest

from whatsbot.domain.transcription import (
    MAX_TRANSCRIPT_CHARS,
    clean_transcript,
)

# --- basic behaviour ------------------------------------------------------


def test_clean_transcript_passes_through_plain_text() -> None:
    raw = "Hallo Claude, wie geht's?"
    assert clean_transcript(raw) == raw


def test_clean_transcript_trims_outer_whitespace() -> None:
    assert clean_transcript("  \n\nhallo\n\n  ") == "hallo"


def test_clean_transcript_empty_string() -> None:
    assert clean_transcript("") == ""


def test_clean_transcript_only_whitespace() -> None:
    assert clean_transcript("   \n\t\n  ") == ""


def test_clean_transcript_non_string_returns_empty() -> None:
    # type: ignore invalid_but_defensive
    assert clean_transcript(None) == ""  # type: ignore[arg-type]
    assert clean_transcript(42) == ""  # type: ignore[arg-type]


# --- whisper bracket annotations ------------------------------------------


@pytest.mark.parametrize(
    "marker",
    [
        "[BLANK_AUDIO]",
        "[Music]",
        "[MUSIC]",
        "[Laughter]",
        "[Applause]",
        "[Silence]",
        "[inaudible]",
        "[INAUDIBLE]",
        "[background noise]",
        "[sound effect]",
        "[sound effects]",
    ],
)
def test_clean_transcript_strips_annotation(marker: str) -> None:
    raw = f"hallo {marker} welt"
    assert clean_transcript(raw) == "hallo  welt" or clean_transcript(raw) == "hallo welt" or clean_transcript(raw) == "hallo   welt"


def test_clean_transcript_strips_multiple_annotations() -> None:
    raw = "[BLANK_AUDIO]\n[Music] hallo [Laughter]"
    out = clean_transcript(raw)
    assert "BLANK_AUDIO" not in out
    assert "Music" not in out
    assert "Laughter" not in out
    assert "hallo" in out


def test_clean_transcript_preserves_non_annotation_brackets() -> None:
    # A user saying "siehe [ChatGPT]" should keep the brackets because
    # "ChatGPT" isn't in our known-annotation list.
    raw = "siehe [ChatGPT] und [BLANK_AUDIO]"
    out = clean_transcript(raw)
    assert "[ChatGPT]" in out
    assert "BLANK_AUDIO" not in out


# --- timestamp line prefixes ----------------------------------------------


def test_clean_transcript_strips_timestamp_prefix() -> None:
    raw = "[00:00:01.000 --> 00:00:04.500] hallo claude"
    assert clean_transcript(raw) == "hallo claude"


def test_clean_transcript_strips_mm_ss_timestamp_prefix() -> None:
    # Some whisper builds use mm:ss only (no hour).
    raw = "[00:01 --> 00:03] kurz gesagt"
    assert clean_transcript(raw) == "kurz gesagt"


def test_clean_transcript_timestamp_on_each_line() -> None:
    raw = (
        "[00:00:00.000 --> 00:00:02.000] Erster Satz.\n"
        "[00:00:02.500 --> 00:00:05.100] Zweiter Satz."
    )
    out = clean_transcript(raw)
    assert "-->" not in out
    assert "Erster Satz." in out
    assert "Zweiter Satz." in out


# --- whitespace collapse --------------------------------------------------


def test_clean_transcript_collapses_blank_lines() -> None:
    raw = "erster\n\n\n\nzweiter\n\n\ndritter"
    out = clean_transcript(raw)
    assert out == "erster\nzweiter\ndritter"


def test_clean_transcript_keeps_single_newlines() -> None:
    raw = "a\nb\nc"
    assert clean_transcript(raw) == "a\nb\nc"


def test_clean_transcript_trims_per_line() -> None:
    raw = "  hallo  \n  welt  "
    assert clean_transcript(raw) == "hallo\nwelt"


# --- truncation ----------------------------------------------------------


def test_clean_transcript_under_limit_unchanged() -> None:
    raw = "a" * (MAX_TRANSCRIPT_CHARS - 1)
    assert clean_transcript(raw) == raw


def test_clean_transcript_at_limit_unchanged() -> None:
    raw = "a" * MAX_TRANSCRIPT_CHARS
    assert clean_transcript(raw) == raw


def test_clean_transcript_over_limit_is_truncated_with_ellipsis() -> None:
    raw = "a" * (MAX_TRANSCRIPT_CHARS + 500)
    out = clean_transcript(raw)
    assert len(out) == MAX_TRANSCRIPT_CHARS
    assert out.endswith("…")
    assert out[:-1] == "a" * (MAX_TRANSCRIPT_CHARS - 1)


def test_clean_transcript_over_limit_after_cleaning() -> None:
    # Big blob of annotations + real content — after stripping we
    # should still truncate if the real content is too long.
    raw = "[BLANK_AUDIO]\n" + "x" * (MAX_TRANSCRIPT_CHARS + 100)
    out = clean_transcript(raw)
    assert len(out) == MAX_TRANSCRIPT_CHARS
    assert out.endswith("…")
