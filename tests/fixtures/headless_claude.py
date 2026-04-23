#!/usr/bin/env python3
"""Headless Claude Code stub for integration tests.

Integration tests launch the tmux session with this script as the
injected ``safe-claude`` binary so we don't need a real Max-20x
subscription to verify that ``SessionService.ensure_started`` starts
a tmux session and persists a ``claude_sessions`` row.

Behaviour:

* Accept (but ignore) the flags the real ``safe-claude`` wrapper
  passes through: ``--resume <id>``, ``--permission-mode <mode>``,
  ``--dangerously-skip-permissions``. Unknown flags are tolerated too
  so callers can layer on extra args without breaking the stub.

* Write a minimal transcript JSONL under
  ``$HEADLESS_CLAUDE_HOME/projects/<encoded-cwd>/sessions/<uuid>.jsonl``
  (or ``$HOME/.claude`` if unset). This is a stand-in for what the
  real Claude Code writes — enough structure that later checkpoints
  (C4.2+) can exercise the transcript watcher against it, but with
  only the fields Phase 4 actually looks at.

* Exit 0 so tmux displays a clean pane after the stub finishes.

Run from a tmux pane like the real ``safe-claude``:

    tmux send-keys -t wb-alpha "safe-claude --resume '' " Enter
"""

from __future__ import annotations

import json
import os
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path


def main(argv: list[str]) -> int:
    # Pull --resume value if present; otherwise mint a new UUID so the
    # transcript has a stable session id for later phases to pick up.
    resume_id = ""
    it = iter(argv[1:])
    for token in it:
        if token == "--resume":
            resume_id = next(it, "")
            break

    session_id = resume_id or uuid.uuid4().hex
    claude_home = Path(os.environ.get("HEADLESS_CLAUDE_HOME", str(Path.home() / ".claude")))
    # Real Claude Code URL-encodes the cwd into the directory name. The
    # exact encoding isn't important for C4.1d; we just need a stable,
    # filesystem-safe transform. Tests that care about the precise
    # path override via env var.
    cwd_label = os.getcwd().replace("/", "-")
    transcript_dir = claude_home / "projects" / cwd_label / "sessions"
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{session_id}.jsonl"

    event = {
        "type": "system",
        "subtype": "stub_started",
        "session_id": session_id,
        "cwd": os.getcwd(),
        "argv": argv[1:],
        "ts": datetime.now(UTC).isoformat(),
    }
    with transcript_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(event) + "\n")

    return 0


if __name__ == "__main__":  # pragma: no cover - entry-point shim
    sys.exit(main(sys.argv))
