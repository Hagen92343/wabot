"""Project domain model — pure, no I/O.

A *project* in whatsbot is a working directory under ``~/projekte/<name>/``
that maps 1:1 to a tmux session, a Claude Code session, and a per-project
mode (normal / strict / yolo). Spec §6 + §11.

Phase 1 didn't touch this — Phase 2 introduces the model and persistence
layer. Phase 4 will hook tmux + Claude into it. The fields below mirror
the ``projects`` table from Spec §19.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum


class Mode(StrEnum):
    """Per-project Claude permission mode (Spec §6)."""

    NORMAL = "normal"
    STRICT = "strict"
    YOLO = "yolo"


class SourceMode(StrEnum):
    """How the project was created."""

    EMPTY = "empty"
    GIT = "git"


_NAME_PATTERN = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
_RESERVED_NAMES: frozenset[str] = frozenset(
    {
        # Common conflict targets — the user types `/new ls` and we'd start
        # confusing ourselves.
        ".",
        "..",
        "_",
        "ls",
        "p",
        "rm",
        "new",
        "info",
        "status",
        "help",
        "ping",
        "trash",
    }
)


class InvalidProjectNameError(ValueError):
    """Raised when a project name fails validation."""


def validate_project_name(name: str) -> str:
    """Return the canonical name or raise ``InvalidProjectNameError``.

    Rules (single-user, filesystem-safe, command-router-safe):
        - 2-32 characters
        - Lowercase letters, digits, ``_`` or ``-``
        - First character must be a letter or digit (no leading underscore)
        - Not a reserved word (commands, ``.``, ``..``, ...)
    """
    if not isinstance(name, str):
        raise InvalidProjectNameError(f"name muss ein String sein, bekam {type(name).__name__}")
    candidate = name.strip()
    if candidate in _RESERVED_NAMES:
        raise InvalidProjectNameError(
            f"'{candidate}' ist reserviert - bitte einen anderen Namen waehlen."
        )
    if not _NAME_PATTERN.fullmatch(candidate):
        raise InvalidProjectNameError(
            f"'{name}' ist kein gueltiger Projektname. "
            f"Erlaubt: 2-32 Zeichen, klein, Ziffern, '_' oder '-', nicht mit '_' beginnend."
        )
    return candidate


@dataclass(frozen=True, slots=True)
class Project:
    """Persisted project record. Mirrors the ``projects`` table."""

    name: str
    source_mode: SourceMode
    created_at: datetime
    source: str | None = None
    last_used_at: datetime | None = None
    default_model: str = "sonnet"
    mode: Mode = Mode.NORMAL

    def __post_init__(self) -> None:
        # Run validation defensively even when constructed from DB rows so a
        # corrupted row can't sneak past the application layer.
        validate_project_name(self.name)


@dataclass(frozen=True, slots=True)
class ProjectListing:
    """A single row in ``/ls`` output. Domain layer formats it via
    ``format_listing`` so the command handler doesn't need to know the
    field order or emoji choices."""

    project: Project
    is_active: bool = False


_MODE_EMOJI = {
    Mode.NORMAL: "🟢",
    Mode.STRICT: "🔵",
    Mode.YOLO: "🔴",
}


def format_listing(listings: list[ProjectListing]) -> str:
    """Render a ``/ls`` reply. Empty list returns a friendly hint."""
    if not listings:
        return (
            "noch keine Projekte. Lege eines an mit:\n"
            "  /new <name>            — leeres Projekt\n"
            "  /new <name> git <url>  — Git-Klon (Phase 2.2)"
        )
    lines = ["Projekte:"]
    for entry in listings:
        marker = "▶" if entry.is_active else " "
        emoji = _MODE_EMOJI.get(entry.project.mode, "·")
        lines.append(
            f"  {marker} {emoji} {entry.project.name:<24} " f"({entry.project.source_mode.value})"
        )
    return "\n".join(lines)
