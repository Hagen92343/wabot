"""Project domain model — pure, no I/O.

A *project* in whatsbot is a working directory that maps 1:1 to a tmux
session, a Claude Code session, and a per-project mode (normal / strict
/ yolo). Spec §6 + §11.

Historically the directory lived under ``~/projekte/<name>/`` always.
Phase 11 adds ``/import`` — importing existing directories at arbitrary
paths. ``Project.path`` holds the explicit absolute path; ``None`` means
"use the default ``projects_root / name``" (Legacy + /new-created).

The fields below mirror the ``projects`` table from Spec §19 after
migration 001.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path


class Mode(StrEnum):
    """Per-project Claude permission mode (Spec §6)."""

    NORMAL = "normal"
    STRICT = "strict"
    YOLO = "yolo"


class SourceMode(StrEnum):
    """How the project entered whatsbot's registry."""

    EMPTY = "empty"
    GIT = "git"
    IMPORTED = "imported"


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
    """Persisted project record. Mirrors the ``projects`` table.

    ``path`` is ``None`` for empty/git projects created via ``/new`` —
    their location is always ``projects_root / name``. For ``/import``
    projects it holds the explicit absolute path on disk. Call
    :func:`resolved_path` to always get the concrete Path regardless of
    storage variant.
    """

    name: str
    source_mode: SourceMode
    created_at: datetime
    source: str | None = None
    last_used_at: datetime | None = None
    default_model: str = "sonnet"
    mode: Mode = Mode.NORMAL
    path: Path | None = None

    def __post_init__(self) -> None:
        # Run validation defensively even when constructed from DB rows so a
        # corrupted row can't sneak past the application layer.
        validate_project_name(self.name)
        if self.path is not None and not self.path.is_absolute():
            raise ValueError(
                f"Project.path must be absolute when set, got {self.path!r}"
            )


def resolved_path(project: Project, projects_root: Path) -> Path:
    """Return the concrete on-disk path of ``project``.

    Imported projects (``source_mode=IMPORTED``) store an explicit
    ``path``. Legacy empty/git projects use ``projects_root / name``.
    Pure function — no filesystem access.
    """
    if project.path is not None:
        return project.path
    return projects_root / project.name


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
            "  /new <name> git <url>  — Git-Klon\n"
            "  /import <name> <path>  — bestehenden Ordner anhaengen"
        )
    lines = ["Projekte:"]
    for entry in listings:
        marker = "▶" if entry.is_active else " "
        emoji = _MODE_EMOJI.get(entry.project.mode, "·")
        source = entry.project.source_mode.value
        suffix = ""
        if entry.project.source_mode is SourceMode.IMPORTED and entry.project.path is not None:
            # Imported projects show their path so the user can tell at
            # a glance which bestehender Ordner got wired up.
            suffix = f" → {entry.project.path}"
        lines.append(
            f"  {marker} {emoji} {entry.project.name:<24} ({source}){suffix}"
        )
    return "\n".join(lines)
