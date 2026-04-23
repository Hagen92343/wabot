"""Unit tests for whatsbot.domain.text_sanitize."""

from __future__ import annotations

import pytest

from whatsbot.domain.text_sanitize import sanitize_inbound_text


def test_plain_ascii_passes_through_unchanged() -> None:
    assert sanitize_inbound_text("hello world") == "hello world"


def test_empty_string_returns_empty() -> None:
    assert sanitize_inbound_text("") == ""


def test_unicode_is_preserved() -> None:
    assert sanitize_inbound_text("Καλημέρα 🚀 日本語") == "Καλημέρα 🚀 日本語"


@pytest.mark.parametrize(
    "allowed",
    ["\t", "\n", "\r", "line1\nline2", "col1\tcol2\r\n"],
)
def test_tab_lf_cr_preserved(allowed: str) -> None:
    assert sanitize_inbound_text(allowed) == allowed


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("hello\x00world", "helloworld"),
        ("\x1b[31mred\x1b[0m", "[31mred[0m"),
        ("bell\x07", "bell"),
        ("back\x08space", "backspace"),
        ("del\x7ftrail", "deltrail"),
        ("\x01\x02\x03", ""),
        ("\x0b\x0c", ""),  # vertical-tab + form-feed also stripped
    ],
)
def test_control_characters_stripped(raw: str, expected: str) -> None:
    assert sanitize_inbound_text(raw) == expected


def test_idempotent() -> None:
    raw = "safe\x00\x07\x1b[hm"
    once = sanitize_inbound_text(raw)
    assert once == sanitize_inbound_text(once)


def test_leaves_clean_input_untouched_by_identity() -> None:
    # Fast-path regression guard: clean input must not trigger the
    # allocation of a new string.
    raw = "no controls here"
    assert sanitize_inbound_text(raw) is raw


def test_emoji_only_prompt_preserved() -> None:
    emoji = "🔥🚀🤖"
    assert sanitize_inbound_text(emoji) == emoji


def test_mixed_german_plus_controls() -> None:
    assert (
        sanitize_inbound_text("Grüße\x00 aus München\x07")
        == "Grüße aus München"
    )


def test_only_controls_strips_to_empty() -> None:
    assert sanitize_inbound_text("\x00\x01\x02\x03\x04") == ""


def test_c1_high_controls_preserved() -> None:
    # U+0080..U+009F are Latin-1 Supplement control chars; we
    # explicitly do not strip them — they're rarely used but the
    # bot should not make value judgements about Unicode.
    assert sanitize_inbound_text("ab") == "ab"
