"""Unit tests for whatsbot.domain.whitelist."""

from __future__ import annotations

import pytest

from whatsbot.domain.whitelist import is_allowed, parse_whitelist

pytestmark = pytest.mark.unit


def test_parse_simple() -> None:
    assert parse_whitelist("+491701234567") == frozenset({"+491701234567"})


def test_parse_multiple_with_whitespace() -> None:
    raw = "+491701234567,  +491775555555 ,+491790000000"
    assert parse_whitelist(raw) == frozenset({"+491701234567", "+491775555555", "+491790000000"})


def test_parse_drops_empty_segments() -> None:
    raw = "+491701234567,,,,+491775555555,"
    assert parse_whitelist(raw) == frozenset({"+491701234567", "+491775555555"})


def test_parse_empty_string_is_empty_set() -> None:
    assert parse_whitelist("") == frozenset()
    assert parse_whitelist("   ") == frozenset()
    assert parse_whitelist(",,,") == frozenset()


def test_parse_dedupes() -> None:
    assert parse_whitelist("+491,+491,+491") == frozenset({"+491"})


def test_is_allowed_match() -> None:
    wl = frozenset({"+491701234567"})
    assert is_allowed("+491701234567", wl) is True


def test_is_allowed_miss() -> None:
    wl = frozenset({"+491701234567"})
    assert is_allowed("+499999999999", wl) is False


def test_is_allowed_against_empty_whitelist_is_false() -> None:
    """Fail-closed: empty whitelist allows nobody."""
    assert is_allowed("+491701234567", frozenset()) is False


def test_is_allowed_is_case_and_format_strict() -> None:
    """Meta delivers consistent international format. We do NOT strip
    a leading '+' or normalize spaces — anything else fails closed."""
    wl = frozenset({"+491701234567"})
    assert is_allowed("491701234567", wl) is False  # missing +
    assert is_allowed(" +491701234567 ", wl) is False  # padded
    assert is_allowed("+49 170 1234567", wl) is False  # with spaces
