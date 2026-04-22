"""Unit tests for whatsbot.domain.output_guard."""

from __future__ import annotations

import pytest

from whatsbot.domain.output_guard import (
    CHUNK_CHARS,
    THRESHOLD_BYTES,
    body_size_bytes,
    chunk_for_whatsapp,
    format_warning,
    is_oversized,
)

pytestmark = pytest.mark.unit


class TestBodySize:
    def test_empty(self) -> None:
        assert body_size_bytes("") == 0

    def test_ascii(self) -> None:
        assert body_size_bytes("hello") == 5

    def test_umlaut_counts_utf8_bytes(self) -> None:
        # Spec §10 talks about "KB senden" — real wire cost. 'ä' is 2
        # bytes in UTF-8, so two umlauts + one ASCII is 5 bytes.
        assert body_size_bytes("ähä") == 5


class TestIsOversized:
    def test_under_threshold(self) -> None:
        assert is_oversized("x" * (THRESHOLD_BYTES - 1)) is False

    def test_at_threshold_still_ok(self) -> None:
        # Threshold is *strictly* greater than — exactly 10 KB is fine.
        assert is_oversized("x" * THRESHOLD_BYTES) is False

    def test_over_threshold(self) -> None:
        assert is_oversized("x" * (THRESHOLD_BYTES + 1)) is True

    def test_custom_threshold(self) -> None:
        assert is_oversized("abc", threshold=2) is True
        assert is_oversized("abc", threshold=3) is False


class TestFormatWarning:
    def test_contains_size_in_kb(self) -> None:
        msg = format_warning(15234, 15234)
        assert "14.9KB" in msg or "14.8KB" in msg  # ~14.9 after /1024

    def test_contains_char_count(self) -> None:
        msg = format_warning(15234, 15234)
        assert "15234 chars" in msg

    def test_mentions_all_three_commands(self) -> None:
        msg = format_warning(15000, 15000)
        assert "/send" in msg
        assert "/discard" in msg
        assert "/save" in msg


class TestChunker:
    def test_short_body_single_chunk_no_prefix(self) -> None:
        chunks = chunk_for_whatsapp("hello world")
        assert chunks == ["hello world"]

    def test_empty_body_returns_single_empty_chunk(self) -> None:
        # Callers expect a non-empty list so the outbound loop still runs.
        assert chunk_for_whatsapp("") == [""]

    def test_long_body_splits_and_numbers(self) -> None:
        body = "a" * (CHUNK_CHARS * 3)
        chunks = chunk_for_whatsapp(body)
        assert len(chunks) == 3
        assert chunks[0].startswith("(1/3)\n")
        assert chunks[1].startswith("(2/3)\n")
        assert chunks[2].startswith("(3/3)\n")
        # The content itself is preserved end-to-end.
        joined = "".join(c.split("\n", 1)[1] for c in chunks)
        assert joined == body

    def test_chunk_size_must_be_positive(self) -> None:
        with pytest.raises(ValueError):
            chunk_for_whatsapp("x", chunk_size=0)

    def test_custom_chunk_size(self) -> None:
        chunks = chunk_for_whatsapp("abcdefgh", chunk_size=3)
        # 8 chars / 3 = 3 chunks: abc, def, gh
        assert len(chunks) == 3
        assert chunks[0] == "(1/3)\nabc"
        assert chunks[1] == "(2/3)\ndef"
        assert chunks[2] == "(3/3)\ngh"
