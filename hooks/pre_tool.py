#!/usr/bin/env python3
"""Pre-Tool-Hook entrypoint for Claude Code.

Claude invokes this script on every Bash/Write/Edit call (registered in
each project's ``.claude/settings.json``). It reads the tool event as
JSON on stdin and must return one of:

* Exit 0 with a JSON allow on stdout — tool runs as-is.
* Exit 0 with a JSON deny on stdout — tool refused, reason shown to user.
* Exit 2 with a reason on stderr — short-circuit deny, preferred when
  something went wrong inside the hook itself.

Fail-closed discipline (Spec §7): anything we can't positively classify
as "allow" becomes Exit 2. The bot process is our only source of truth;
if the bot isn't reachable we refuse.

Read-only tools (Read/Grep/Glob) are short-circuited to Exit 0 without
any HTTP call — they're never in scope for hook gating.
"""

from __future__ import annotations

import json
import sys
from typing import Any

from hooks._common import HookError, call_hook, load_shared_secret

_READ_ONLY_TOOLS: frozenset[str] = frozenset({"Read", "Grep", "Glob"})


def _die(reason: str) -> int:
    """Print ``reason`` to stderr and return Exit 2."""
    print(f"whatsbot hook: {reason}", file=sys.stderr)
    return 2


def _emit_allow(reason: str = "") -> int:
    payload = {
        "hookSpecificOutput": {
            "permissionDecision": "allow",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    return 0


def _emit_deny(reason: str) -> int:
    payload = {
        "hookSpecificOutput": {
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }
    print(json.dumps(payload))
    return 0  # Claude treats this as a deny, we exit clean.


def main(argv: list[str] | None = None) -> int:
    # argv is unused — stdin is the Claude-hook contract. Accepting it
    # keeps the signature testable without monkeypatching sys.argv.
    del argv

    try:
        raw = sys.stdin.read()
    except Exception as exc:  # noqa: BLE001
        return _die(f"could not read stdin: {exc}")
    if not raw.strip():
        return _die("empty stdin")

    try:
        event = json.loads(raw)
    except json.JSONDecodeError as exc:
        return _die(f"stdin is not JSON: {exc}")
    if not isinstance(event, dict):
        return _die("stdin JSON is not an object")

    tool = event.get("tool")
    if not isinstance(tool, str):
        return _die("event missing 'tool' string")

    # Read-only tools skip the hook entirely (Spec §7).
    if tool in _READ_ONLY_TOOLS:
        return _emit_allow(f"{tool} bypass")

    tool_input = event.get("tool_input")
    if not isinstance(tool_input, dict):
        return _die(f"event missing 'tool_input' dict for {tool}")

    session_id = event.get("session_id") if isinstance(event.get("session_id"), str) else None

    try:
        secret = load_shared_secret()
    except HookError as exc:
        return _die(str(exc))

    if tool == "Bash":
        return _handle_bash(tool_input, session_id=session_id, secret=secret)
    if tool in ("Write", "Edit"):
        return _handle_write(tool_input, session_id=session_id, secret=secret)

    # Unknown tool: fail-closed by default. Claude may introduce new
    # tools over time — when it does, we want to notice instead of
    # silently letting them through.
    return _die(f"unknown tool {tool!r}")


def _handle_bash(
    tool_input: dict[str, Any], *, session_id: str | None, secret: str
) -> int:
    command = tool_input.get("command")
    if not isinstance(command, str) or not command.strip():
        return _die("Bash tool_input missing 'command' string")

    try:
        resp = call_hook(
            path="/hook/bash",
            payload={
                "command": command,
                "session_id": session_id,
            },
            secret=secret,
        )
    except HookError as exc:
        return _die(str(exc))

    if resp.is_allow:
        return _emit_allow(resp.reason)
    return _emit_deny(resp.reason)


def _handle_write(
    tool_input: dict[str, Any], *, session_id: str | None, secret: str
) -> int:
    # Claude's Write/Edit tools use the ``file_path`` key. We normalise
    # to ``path`` before passing it to the bot.
    path = tool_input.get("file_path") or tool_input.get("path")
    if not isinstance(path, str) or not path.strip():
        return _die("Write/Edit tool_input missing 'file_path' string")

    try:
        resp = call_hook(
            path="/hook/write",
            payload={
                "path": path,
                "session_id": session_id,
            },
            secret=secret,
        )
    except HookError as exc:
        return _die(str(exc))

    if resp.is_allow:
        return _emit_allow(resp.reason)
    return _emit_deny(resp.reason)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
