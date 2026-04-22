"""Allow-Rule pattern parsing + validation — pure domain.

Spec §6 / §12. Patterns look like ``Bash(npm test)`` or ``Read(~/projekte/**)``.
We accept any of the six tools Claude Code understands (Bash / Write / Edit
/ Read / Grep / Glob); the inner pattern is opaque to the bot — Claude
itself decides whether a given concrete invocation matches.

Functions here are pure: they parse and format strings, they don't touch
the DB or the per-project ``.claude/settings.json``. The Repository +
SettingsWriter handle that.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

# Tools Claude Code recognises in `permissions.allow` / `permissions.deny`.
ALLOWED_TOOLS: Final[frozenset[str]] = frozenset({"Bash", "Write", "Edit", "Read", "Grep", "Glob"})


class AllowRuleSource(StrEnum):
    """Where a rule came from. Matches the CHECK constraint on the
    ``allow_rules.source`` column (Spec §19)."""

    DEFAULT = "default"
    SMART_DETECTION = "smart_detection"
    MANUAL = "manual"


@dataclass(frozen=True, slots=True)
class AllowRulePattern:
    """A parsed Tool(pattern) pair, ready to persist or render."""

    tool: str
    pattern: str


class InvalidAllowRuleError(ValueError):
    """Raised when an Allow-Rule string is malformed."""


# Tool name + parenthesised pattern, with optional surrounding whitespace.
# We require ``Tool(...)`` — bare ``npm test`` is rejected as ambiguous.
_PATTERN_RE = re.compile(
    r"^\s*([A-Za-z][A-Za-z0-9]*)\s*\((.+)\)\s*$",
    re.DOTALL,
)


def parse_pattern(raw: str) -> AllowRulePattern:
    """Parse ``Tool(pattern)`` (with optional surrounding whitespace) into
    an ``AllowRulePattern``. Raises ``InvalidAllowRuleError`` if the input
    isn't shaped right or names an unknown tool.

    The inner pattern can contain anything except an unbalanced trailing
    ``)`` — we do NOT try to parse globs / regexes here, that's Claude
    Code's job at execution time.
    """
    if not isinstance(raw, str):
        raise InvalidAllowRuleError(f"Allow-Rule muss ein String sein, bekam {type(raw).__name__}")
    match = _PATTERN_RE.fullmatch(raw)
    if match is None:
        raise InvalidAllowRuleError(
            f"'{raw}' ist keine valide Allow-Rule. " f"Format: Tool(pattern) — z.B. Bash(npm test)"
        )
    tool, pattern = match.group(1), match.group(2).strip()
    if tool not in ALLOWED_TOOLS:
        raise InvalidAllowRuleError(f"Unbekanntes Tool '{tool}'. Erlaubt: {sorted(ALLOWED_TOOLS)}")
    if not pattern:
        raise InvalidAllowRuleError(f"'{raw}' hat einen leeren Pattern-Teil zwischen den Klammern.")
    # Reject patterns that themselves contain an unescaped trailing ')',
    # since the regex .+ would consume it greedily and leave you with
    # confusing matches like 'Bash(echo (hi)))' parsing as 'echo (hi))'.
    if pattern.count("(") != pattern.count(")"):
        raise InvalidAllowRuleError(f"Unbalancierte Klammern im Pattern: {pattern!r}")
    return AllowRulePattern(tool=tool, pattern=pattern)


def format_pattern(rule: AllowRulePattern) -> str:
    """Render an ``AllowRulePattern`` as ``Tool(pattern)``."""
    return f"{rule.tool}({rule.pattern})"


def patterns_equal(a: AllowRulePattern, b: AllowRulePattern) -> bool:
    """Equality for de-duplication. Tools and patterns compare exactly
    (case-sensitive) — Claude Code itself is case-sensitive on these."""
    return a.tool == b.tool and a.pattern == b.pattern
