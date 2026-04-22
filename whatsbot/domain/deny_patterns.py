"""Deny-pattern matching — pure domain.

Spec §12 lists 17 bash patterns that must be blocked in every mode,
including YOLO (``--dangerously-skip-permissions``). These patterns
represent commands whose failure modes are irreversible and severe
enough that even the bot's owner should have to explicitly override
them with a PIN confirmation.

This module is pure: it takes a command string, returns either a
``DenyMatch`` with the offending pattern + reason, or ``None``. No
state, no I/O. Matching is robust against two classes of evasion:

1. Whitespace tricks — ``rm  -rf   /`` collapses to ``rm -rf /``.
2. Simple quoting — ``rm -rf "/"`` and ``rm -rf '/'`` strip to
   ``rm -rf /``.

More aggressive evasion (``bash -c '...'`` wrapping, command chaining
with ``&&``/``;``, alias-piggybacking, base64 payloads) is out of scope
for this layer. Spec §12 names these 17 literal patterns, and Claude
Code doesn't normally emit the obfuscated variants. Defence in depth
means this layer is one of four — not the only one.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass
from typing import Final


@dataclass(frozen=True, slots=True)
class DenyPattern:
    """A single pattern with the reason shown to the user on deny."""

    pattern: str  # fnmatch-style glob, applied to the normalised command
    reason: str  # short, user-facing — flows to stderr and WhatsApp


@dataclass(frozen=True, slots=True)
class DenyMatch:
    """Emitted when a command matches a deny pattern."""

    pattern: DenyPattern
    normalized_command: str


# Spec §12 — the 17 deny patterns. Order matters only for readability;
# the matcher walks top-to-bottom and returns the first hit.
DENY_PATTERNS: Final[tuple[DenyPattern, ...]] = (
    DenyPattern("rm -rf /", "deletes filesystem root"),
    DenyPattern("rm -rf ~", "deletes home directory"),
    DenyPattern("rm -rf ..", "deletes parent directory recursively"),
    DenyPattern("sudo *", "privilege escalation"),
    DenyPattern("git push --force*", "force-push rewrites published history"),
    DenyPattern("git reset --hard*", "destroys uncommitted work"),
    DenyPattern("git clean -fd*", "deletes untracked files and dirs"),
    DenyPattern("docker system prune*", "wipes docker resources"),
    DenyPattern("docker volume rm*", "deletes docker volumes"),
    DenyPattern("chmod 777 *", "world-writable permissions"),
    DenyPattern("curl * | sh", "pipes remote code to shell"),
    DenyPattern("curl * | bash", "pipes remote code to bash"),
    DenyPattern("wget * | sh", "pipes remote code to shell"),
    DenyPattern("wget * | bash", "pipes remote code to bash"),
    DenyPattern("bash /tmp/*", "executes scripts from /tmp"),
    DenyPattern("sh /tmp/*", "executes scripts from /tmp"),
    DenyPattern("zsh /tmp/*", "executes scripts from /tmp"),
)


_WHITESPACE_RE: Final = re.compile(r"\s+")

# Strip matching single-or-double quotes around whitespace-free tokens.
# We intentionally don't try to handle escapes or mixed-quote content —
# this is a hardening nudge, not a shell parser.
_QUOTED_TOKEN_RE: Final = re.compile(r"""(?P<q>['"])([^'"\s]*)(?P=q)""")


def normalize_command(command: str) -> str:
    """Return a canonical form suitable for glob-matching.

    Collapses internal whitespace to single spaces, then strips matching
    single/double quotes around tokens that contain no whitespace or
    other quotes. Everything else is preserved.
    """
    trimmed = _WHITESPACE_RE.sub(" ", command.strip())
    return _QUOTED_TOKEN_RE.sub(lambda m: m.group(2), trimmed)


def match_bash_command(command: str) -> DenyMatch | None:
    """Return the first matching ``DenyPattern`` (or ``None``).

    Matching is fnmatch-style: ``*`` matches any sequence of characters
    (including whitespace), ``?`` matches any single character.
    Case-sensitive — shells are case-sensitive on these commands.
    """
    if not command or not command.strip():
        return None
    normalized = normalize_command(command)
    for pattern in DENY_PATTERNS:
        if fnmatch.fnmatchcase(normalized, pattern.pattern):
            return DenyMatch(pattern=pattern, normalized_command=normalized)
    return None
