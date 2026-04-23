"""Path helpers for locating Claude Code transcript files.

Claude Code writes one JSONL transcript per session under::

    <claude_home>/projects/<encoded-cwd>/<session-uuid>.jsonl

where ``claude_home`` defaults to ``~/.claude`` and
``encoded-cwd`` is the absolute cwd with forward slashes replaced by
hyphens (verified against live transcripts — see the sample list in
``~/.claude/projects``). Spaces and other characters stay literal.

This module is pure — the ``find_latest_transcript_since`` helper
does call ``Path.iterdir`` + ``Path.stat``, but those are read-only
queries and tests exercise them against a ``tmp_path``. No file
writes, no subprocesses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Final

DEFAULT_CLAUDE_HOME: Final[Path] = Path.home() / ".claude"


def encode_cwd(cwd: Path) -> str:
    """Return the directory name Claude Code uses for ``cwd``.

    The encoding is: resolve to absolute, then replace every
    forward slash with a hyphen. The leading slash becomes a
    leading hyphen, so ``/Users/foo/bar`` → ``-Users-foo-bar``.
    """
    absolute = cwd if cwd.is_absolute() else cwd.resolve()
    return str(absolute).replace("/", "-")


def claude_projects_dir(
    cwd: Path, *, claude_home: Path = DEFAULT_CLAUDE_HOME
) -> Path:
    """Return the per-cwd directory under ``<claude_home>/projects``."""
    return claude_home / "projects" / encode_cwd(cwd)


def expected_transcript_path(
    cwd: Path,
    session_id: str,
    *,
    claude_home: Path = DEFAULT_CLAUDE_HOME,
) -> Path:
    """Return the file a ``--resume <session_id>`` Claude would append to.

    Requires a non-empty ``session_id`` — callers that don't have
    one yet should use ``find_latest_transcript_since`` on the
    projects directory instead.
    """
    if not session_id:
        raise ValueError("session_id must be non-empty")
    return claude_projects_dir(cwd, claude_home=claude_home) / f"{session_id}.jsonl"


def find_latest_transcript_since(
    projects_dir: Path, *, since_mtime: float | None = None
) -> Path | None:
    """Return the newest ``*.jsonl`` in ``projects_dir`` whose mtime
    is ≥ ``since_mtime``, or ``None`` if nothing matches.

    Used to discover which transcript Claude picked when we started
    it without an explicit ``--resume`` (fresh sessions mint their
    own UUID; we only learn the name once the first event is
    written). ``since_mtime`` filters out the thousand stale
    transcripts left over from prior sessions — pass in
    ``time.time()`` captured just before the ``safe-claude`` launch.

    Missing directory yields ``None``.
    """
    if not projects_dir.exists() or not projects_dir.is_dir():
        return None

    newest: Path | None = None
    newest_mtime: float = float("-inf")
    for entry in projects_dir.iterdir():
        if entry.suffix != ".jsonl" or not entry.is_file():
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        if since_mtime is not None and mtime < since_mtime:
            continue
        if mtime > newest_mtime:
            newest_mtime = mtime
            newest = entry
    return newest


def extract_session_id(transcript_path: Path) -> str:
    """The session UUID is the filename stem.

    Kept as a helper so callers don't open-code the filename-to-id
    extraction — if Claude ever changes the layout, this is the
    single place to adapt.
    """
    return transcript_path.stem
