"""Write/Edit path-rules — pure, no I/O.

Spec §12 Layer 3. When Claude Code tries to Write or Edit a file,
the Pre-Tool-Hook queries this module to decide whether the path
is inside an allowed scope, a protected scope, or something that
depends on the project's mode.

Allowed scopes (Spec §12):
    ~/projekte/<current>/            → ``project`` scope, always allow
    /tmp/ and /private/tmp/          → ``temp`` scope, always allow

Protected segments (deny in all modes — Spec §12 Layer 3):
    .git/                            → Git internals
    .vscode/                         → IDE config
    .idea/                           → IDE config
    .claude/                         → Claude Code config
        except .claude/commands/     → user customisations
              .claude/agents/
              .claude/skills/

Any other path:
    Normal → AskUser (PIN round-trip)
    Strict → Deny silently
    YOLO   → Allow

Protected paths win over allowed scopes: a ``.git/`` write inside
``~/projekte/<current>/`` is still blocked. That's the whole point
of the layer — Git internals shouldn't be rewritten by accident
regardless of where they live.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Final

from whatsbot.domain.hook_decisions import HookDecision, allow, ask_user, deny
from whatsbot.domain.projects import Mode

_PROTECTED_SEGMENTS: Final[frozenset[str]] = frozenset(
    {".git", ".vscode", ".idea"}
)
_ALLOWED_CLAUDE_SUBDIRS: Final[frozenset[str]] = frozenset(
    {"commands", "agents", "skills"}
)

# macOS resolves /tmp to /private/tmp; real-world Claude Code writes
# come through either form depending on whether the symlink got
# walked. Accept both.
_TEMP_PREFIXES: Final[tuple[Path, ...]] = (
    Path("/tmp"),  # noqa: S108 — matching against, not creating, temp paths
    Path("/private/tmp"),
)


class PathCategory(StrEnum):
    """Classification output of ``classify_path`` — what kind of
    location is this path relative to the active project."""

    PROJECT = "project"
    TEMP = "temp"
    PROTECTED = "protected"
    OTHER = "other"


def classify_path(
    target: Path, *, project_cwd: Path | None
) -> PathCategory:
    """Categorise ``target`` relative to the allowed / protected
    scopes.

    Paths are normalised to absolute before comparison — relative
    inputs are resolved against the current working directory,
    which in production is the tmux pane's cwd (i.e. ``project_cwd``).
    The function is still pure: ``Path.resolve()`` just normalises
    without touching the filesystem for canonicalisation (we use
    ``strict=False`` semantics by default).
    """
    absolute = target if target.is_absolute() else Path.cwd() / target

    # Protected comes first so a .git under project_cwd still denies.
    if _is_protected(absolute):
        return PathCategory.PROTECTED

    if project_cwd is not None:
        project_cwd_abs = (
            project_cwd if project_cwd.is_absolute() else project_cwd.resolve()
        )
        try:
            if absolute.is_relative_to(project_cwd_abs):
                return PathCategory.PROJECT
        except ValueError:
            pass

    for prefix in _TEMP_PREFIXES:
        try:
            if absolute.is_relative_to(prefix):
                return PathCategory.TEMP
        except ValueError:
            pass

    return PathCategory.OTHER


def evaluate_write(
    target: Path,
    *,
    project_cwd: Path | None,
    mode: Mode,
) -> HookDecision:
    """Return the hook decision for writing to ``target``.

    The ``mode`` parameter only matters for ``OTHER`` paths — the
    four decision rules collapse to three category-specific answers
    for the first three categories.
    """
    category = classify_path(target, project_cwd=project_cwd)

    if category is PathCategory.PROTECTED:
        return deny(f"write to protected path denied: {target}")

    if category is PathCategory.PROJECT:
        return allow("project scope")

    if category is PathCategory.TEMP:
        return allow("temp scope")

    # OTHER
    if mode is Mode.STRICT:
        return deny(f"strict: writes outside allowed scope denied: {target}")
    if mode is Mode.YOLO:
        return allow("yolo: writes outside scope allowed")
    return ask_user(f"write outside allowed scope: {target}")


# ---- internals ------------------------------------------------------


def _is_protected(path: Path) -> bool:
    """Walk ``path.parts`` looking for protected segments.

    ``.claude/`` segments are protected *unless* the very next
    segment is one of ``commands``, ``agents``, ``skills`` —
    Spec §12 carves those out explicitly because they're the
    user-customisation entry points.
    """
    parts = path.parts
    for index, part in enumerate(parts):
        if part in _PROTECTED_SEGMENTS:
            return True
        if part == ".claude":
            if (
                index + 1 < len(parts)
                and parts[index + 1] in _ALLOWED_CLAUDE_SUBDIRS
            ):
                # This ``.claude`` is the sanctioned customisation
                # entrypoint — skip past it and keep scanning for
                # other protected segments further down the path.
                continue
            return True
    return False
