"""Per-project ``.claude/settings.json`` writer.

Claude Code reads ``permissions.allow`` / ``permissions.deny`` arrays
from this file (Spec §7). The bot keeps the DB ``allow_rules`` table as
the source of truth and re-renders this file on every change. Other keys
present in the file (``hooks``, project-specific overrides) are
preserved — we only touch ``permissions.allow``.

The file is rewritten atomically (write-tmp + rename) so a Claude-Code
session reading it concurrently never sees half-written JSON.
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any

from whatsbot.domain.allow_rules import AllowRulePattern, format_pattern


def _settings_path(project_dir: Path) -> Path:
    return project_dir / ".claude" / "settings.json"


def load_settings(project_dir: Path) -> dict[str, Any]:
    """Return the parsed ``.claude/settings.json`` or an empty dict if
    the file is absent / unparsable. Defensive — we never want this
    helper to abort a /allow command just because the file got corrupted."""
    path = _settings_path(project_dir)
    if not path.is_file():
        return {}
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_allow_rules(project_dir: Path, patterns: list[AllowRulePattern]) -> Path:
    """Replace ``permissions.allow`` with the rendered patterns.

    Existing top-level keys (``hooks``, ``permissions.deny``, etc.) and
    other ``permissions`` sub-keys are preserved. Only the ``allow`` array
    is rewritten.

    Returns the resolved settings.json path.
    """
    target = _settings_path(project_dir)
    target.parent.mkdir(parents=True, exist_ok=True)

    settings = load_settings(project_dir)
    permissions = settings.get("permissions")
    if not isinstance(permissions, dict):
        permissions = {}
    permissions["allow"] = [format_pattern(p) for p in patterns]
    settings["permissions"] = permissions

    payload = json.dumps(settings, indent=2) + "\n"

    # Atomic write: tmp file in same dir → rename. POSIX rename is atomic
    # within a filesystem so concurrent readers never see a partial write.
    fd, tmp_path = tempfile.mkstemp(prefix=".settings.", suffix=".json.tmp", dir=target.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup if rename failed (e.g. cross-fs).
        with contextlib.suppress(OSError):
            os.unlink(tmp_path)
        raise
    return target
