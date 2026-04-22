"""Command router — pure domain logic for the C1.5 command set.

Phase-1 supports only three commands. Project-management commands (``/new``,
``/ls``, ``/p`` etc.) come in Phase 2 once the project store exists, and
Claude-driven prompts come in Phase 4. Anything not in the table below is
``UnknownCommand`` for now — Phase 4 will turn that into "forward to active
project's Claude session".
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class StatusSnapshot:
    """Inputs the ``/status`` reply needs. Filled by the application layer
    (which knows about uptime and DB state); the domain just formats."""

    version: str
    uptime_seconds: float
    db_ok: bool
    env: str


@dataclass(frozen=True, slots=True)
class CommandResult:
    """Outcome of routing one inbound text. ``reply`` may be empty when the
    command intentionally produces no user-visible output."""

    reply: str
    command: str  # e.g. "/ping" or "<unknown>" — used for log_event tagging


_HELP_TEXT = (
    "verfügbare Commands (Phase 1):\n"
    "  /ping    — Echo + Version + Uptime\n"
    "  /status  — Systemstatus\n"
    "  /help    — diese Liste\n"
    "\n"
    "Mehr kommt mit den nächsten Phasen (siehe SPEC.md §11)."
)


def route(text: str, snapshot: StatusSnapshot) -> CommandResult:
    """Route a single inbound text to a reply.

    Whitespace is stripped on both sides. Commands are case-sensitive (Spec
    §11 lists them lower-case). Unknown commands return a friendly hint —
    they do NOT raise, because Phase 4 will replace this branch with
    "forward to Claude".
    """
    cmd = text.strip()

    if cmd == "/ping":
        return CommandResult(
            reply=f"pong · v{snapshot.version} · uptime {round(snapshot.uptime_seconds)}s",
            command="/ping",
        )

    if cmd == "/status":
        db_marker = "ok" if snapshot.db_ok else "DEGRADED"
        return CommandResult(
            reply=(
                f"whatsbot v{snapshot.version}\n"
                f"  env:    {snapshot.env}\n"
                f"  uptime: {round(snapshot.uptime_seconds)}s\n"
                f"  db:     {db_marker}"
            ),
            command="/status",
        )

    if cmd == "/help":
        return CommandResult(reply=_HELP_TEXT, command="/help")

    return CommandResult(
        reply=(f"unbekanntes Kommando {cmd!r}. Tippe /help für die Liste."),
        command="<unknown>",
    )
