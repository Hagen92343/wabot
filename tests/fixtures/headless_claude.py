#!/usr/bin/env python3
"""Headless Claude Code stub for integration tests.

Integration tests launch the tmux session with this script as the
injected ``safe-claude`` binary so we don't need a real Max-20x
subscription to verify that the transcript + ingest + WhatsApp-
delivery path works end-to-end.

Behaviour:

* Accept (but ignore) the flags the real ``safe-claude`` wrapper
  passes through: ``--resume <id>``, ``--permission-mode <mode>``,
  ``--dangerously-skip-permissions``. Unknown flags are tolerated too
  so callers can layer on extra args without breaking the stub.

* Write a minimal transcript JSONL in the same layout the real
  Claude Code uses::

      $HEADLESS_CLAUDE_HOME/projects/<encoded-cwd>/<session-uuid>.jsonl

  (or ``~/.claude`` if ``HEADLESS_CLAUDE_HOME`` is unset). The
  encoded cwd is the absolute cwd with forward slashes replaced
  by hyphens — matching ``domain.claude_paths.encode_cwd``.

* Emit two events: a ``system`` ``stub_started`` marker (so debug
  tooling can tell the transcript is from the stub) and an
  ``assistant`` event with text content. The assistant event has
  no ``tool_use`` blocks, so ``should_emit_turn_end`` fires
  immediately on the ingest side.

* The assistant text defaults to ``"stub response"`` but can be
  overridden via the ``HEADLESS_CLAUDE_REPLY`` environment
  variable so tests can assert on a marker string.

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
    claude_home = Path(
        os.environ.get("HEADLESS_CLAUDE_HOME", str(Path.home() / ".claude"))
    )
    encoded_cwd = os.getcwd().replace("/", "-")
    # Real Claude Code: projects/<encoded-cwd>/<session-uuid>.jsonl
    # (no intermediate "sessions/" subdir — verified against live
    # transcripts in ~/.claude/projects).
    transcript_dir = claude_home / "projects" / encoded_cwd
    transcript_dir.mkdir(parents=True, exist_ok=True)
    transcript_path = transcript_dir / f"{session_id}.jsonl"

    assistant_reply = os.environ.get("HEADLESS_CLAUDE_REPLY", "stub response")
    now = datetime.now(UTC).isoformat()

    events = [
        {
            "type": "system",
            "subtype": "stub_started",
            "session_id": session_id,
            "cwd": os.getcwd(),
            "argv": argv[1:],
            "uuid": f"s-{session_id}",
            "timestamp": now,
        },
        {
            "type": "assistant",
            "uuid": f"a-{session_id}",
            "timestamp": now,
            "isSidechain": False,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": assistant_reply}],
                "usage": {
                    "input_tokens": 10,
                    "output_tokens": len(assistant_reply),
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 0,
                },
            },
        },
    ]

    with transcript_path.open("a", encoding="utf-8") as fh:
        for event in events:
            fh.write(json.dumps(event) + "\n")
        fh.flush()

    return 0


if __name__ == "__main__":  # pragma: no cover - entry-point shim
    sys.exit(main(sys.argv))
