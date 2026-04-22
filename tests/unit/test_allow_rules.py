"""Unit tests for whatsbot.domain.allow_rules."""

from __future__ import annotations

import pytest

from whatsbot.domain.allow_rules import (
    ALLOWED_TOOLS,
    AllowRulePattern,
    AllowRuleSource,
    InvalidAllowRuleError,
    format_pattern,
    parse_pattern,
    patterns_equal,
)

pytestmark = pytest.mark.unit


# --- ALLOWED_TOOLS ---------------------------------------------------------


def test_allowed_tools_is_the_six_we_documented() -> None:
    """Adding a tool here is a security regression — must be deliberate."""
    assert frozenset({"Bash", "Write", "Edit", "Read", "Grep", "Glob"}) == ALLOWED_TOOLS


# --- parse_pattern: happy paths -------------------------------------------


@pytest.mark.parametrize(
    "raw,tool,pattern",
    [
        ("Bash(npm test)", "Bash", "npm test"),
        ("Bash(git status)", "Bash", "git status"),
        ("Read(~/projekte/**)", "Read", "~/projekte/**"),
        ("Edit(src/*.py)", "Edit", "src/*.py"),
        ("  Bash(make build)  ", "Bash", "make build"),  # padding ok
        ("Write(README.md)", "Write", "README.md"),
        # Inner spaces, dashes, slashes — Claude Code interprets, we don't.
        ("Bash(docker compose up -d)", "Bash", "docker compose up -d"),
    ],
)
def test_parse_extracts_tool_and_pattern(raw: str, tool: str, pattern: str) -> None:
    parsed = parse_pattern(raw)
    assert parsed.tool == tool
    assert parsed.pattern == pattern


# --- parse_pattern: rejection ---------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "npm test",  # no Tool() wrapping
        "Bash npm test",  # no parens
        "Bash()",  # empty pattern
        "Bash( )",  # whitespace-only pattern
        "(npm test)",  # missing tool
        "lowercase(npm)",  # tool not in whitelist (also lowercase)
        "AllTheThings(npm)",  # unknown tool
        "BashEcho(hi)",  # bare alphanumeric extension is also unknown
    ],
)
def test_parse_rejects_garbage(raw: str) -> None:
    with pytest.raises(InvalidAllowRuleError):
        parse_pattern(raw)


def test_parse_rejects_unbalanced_parens() -> None:
    with pytest.raises(InvalidAllowRuleError, match="Klammern"):
        parse_pattern("Bash(echo (hi)")


def test_parse_rejects_non_string() -> None:
    with pytest.raises(InvalidAllowRuleError):
        parse_pattern(None)  # type: ignore[arg-type]
    with pytest.raises(InvalidAllowRuleError):
        parse_pattern(42)  # type: ignore[arg-type]


# --- format_pattern -------------------------------------------------------


def test_format_roundtrips_with_parse() -> None:
    pat = parse_pattern("Bash(npm test)")
    assert format_pattern(pat) == "Bash(npm test)"


def test_format_for_complex_pattern() -> None:
    pat = AllowRulePattern(tool="Bash", pattern="git diff --stat HEAD~10")
    assert format_pattern(pat) == "Bash(git diff --stat HEAD~10)"


# --- patterns_equal -------------------------------------------------------


def test_patterns_equal_same_inputs() -> None:
    a = AllowRulePattern(tool="Bash", pattern="npm test")
    b = AllowRulePattern(tool="Bash", pattern="npm test")
    assert patterns_equal(a, b)


def test_patterns_equal_is_case_sensitive() -> None:
    """Claude Code patterns are case-sensitive — we mirror that."""
    a = AllowRulePattern(tool="Bash", pattern="npm test")
    b = AllowRulePattern(tool="Bash", pattern="NPM TEST")
    assert not patterns_equal(a, b)


def test_patterns_equal_distinguishes_tools() -> None:
    a = AllowRulePattern(tool="Bash", pattern="ls")
    b = AllowRulePattern(tool="Read", pattern="ls")
    assert not patterns_equal(a, b)


# --- AllowRuleSource enum sanity ------------------------------------------


def test_source_values_match_db_check_constraint() -> None:
    """Spec §19: allow_rules.source CHECK(IN ('default','smart_detection','manual'))"""
    assert {s.value for s in AllowRuleSource} == {
        "default",
        "smart_detection",
        "manual",
    }
