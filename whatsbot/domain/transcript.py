"""Claude Code transcript-event parser — pure, no I/O.

Claude Code writes one JSON object per line into
``~/.claude/projects/<encoded-cwd>/<session-uuid>.jsonl`` as the
session progresses. Phase 4 consumes these events in two places:

1. ``application/transcript_ingest.py`` — watches the file and calls
   into this module on every new line to decide whether a Claude
   turn has ended, how many tokens it cost, and what text to ship
   back to WhatsApp.
2. Tests — replay recorded fixtures through the same parser so we
   can verify end-of-turn detection without spinning up a real
   Claude session.

This module is intentionally permissive: unknown event types and
malformed JSON lines fall through to ``None`` / ``UnknownEvent``
rather than raising. The real transcripts carry a long tail of
bookkeeping events (``permission-mode``, ``attachment``,
``file-history-snapshot``, ``last-prompt``) that Phase 4 doesn't
need to interpret — we just skip them.

Spec references: §7 (Stop-Detection via Transcript-Watching),
§8 (Token-Counts aus ``message.usage``), §9 (Bot-Prefix
Zero-Width-Space), §14 (Max-Limit aus Error-Events).
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Final

# Zero-Width-Space. Spec §9: the bot prefixes its own prompts with
# U+200B so the transcript watcher can tell bot-sent user turns from
# those the human typed at the local terminal.
BOT_PREFIX: Final[str] = "​"


# ---- Event dataclasses -----------------------------------------------


@dataclass(frozen=True, slots=True)
class TokenUsage:
    """The four token counters Claude reports per assistant message.

    Phase 4 sums ``input + output + cache_creation + cache_read`` to
    drive the context-fill ratio. Individual fields stay available in
    case Phase 8 wants to break them out in ``/metrics``.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True, slots=True)
class UserEvent:
    """One ``"type": "user"`` line.

    ``text`` is the flattened textual prompt: for string-typed
    ``message.content`` it's that string verbatim, for list-typed
    content it's the concatenation of every ``text``-typed block
    (tool-result blocks are dropped — they're Claude's view of tool
    output, not a prompt the human wrote).
    """

    uuid: str
    timestamp: str
    text: str
    is_sidechain: bool = False
    is_api_error: bool = False
    is_bot_prefixed: bool = False


@dataclass(frozen=True, slots=True)
class AssistantEvent:
    """One ``"type": "assistant"`` line.

    ``text`` is the concatenation of all ``text``-typed blocks in
    ``message.content`` — skipping ``thinking`` and ``tool_use``.
    ``has_tool_use`` is True iff at least one block in that content
    array has ``type == "tool_use"``; the turn-end detector uses it
    to distinguish "Claude is done" from "Claude is mid-tool-call".
    """

    uuid: str
    timestamp: str
    text: str
    usage: TokenUsage
    has_tool_use: bool
    is_sidechain: bool = False
    is_api_error: bool = False


@dataclass(frozen=True, slots=True)
class UsageLimitEvent:
    """Claude-reported Max-limit-hit.

    Spec §14: Claude writes an ``error``-typed event with
    ``subtype == "usage_limit_reached"`` and a ``reset_at`` field.
    Phase 4 just surfaces the structure; Phase 8 wires it into the
    ``max_limits`` table.
    """

    uuid: str
    timestamp: str
    reset_at: str | None = None
    limit_kind: str | None = None


@dataclass(frozen=True, slots=True)
class SystemEvent:
    """Claude-side system message (stop reasons, hook notices)."""

    uuid: str
    timestamp: str
    subtype: str
    is_sidechain: bool = False


@dataclass(frozen=True, slots=True)
class UnknownEvent:
    """Top-level event type we don't recognise (or an event that
    parsed but failed field extraction). Callers normally skip it
    but we return the raw payload for diagnostics."""

    type: str
    raw: dict[str, Any] = field(default_factory=dict)


TranscriptEvent = (
    UserEvent
    | AssistantEvent
    | UsageLimitEvent
    | SystemEvent
    | UnknownEvent
)


# ---- Parser ----------------------------------------------------------


def parse_line(line: str) -> TranscriptEvent | None:
    """Parse one JSONL line into a typed event.

    Returns ``None`` for empty lines or malformed JSON; returns an
    ``UnknownEvent`` for event types we don't care about yet. Never
    raises — the ingest loop must stay alive even if Claude changes
    its schema mid-session.
    """
    stripped = line.strip()
    if not stripped:
        return None
    try:
        raw = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    event_type = raw.get("type")
    if event_type == "user":
        return _parse_user(raw)
    if event_type == "assistant":
        return _parse_assistant(raw)
    if event_type == "error":
        return _parse_error(raw)
    if event_type == "system":
        return _parse_system(raw)
    if isinstance(event_type, str):
        return UnknownEvent(type=event_type, raw=raw)
    return None


def _parse_user(raw: dict[str, Any]) -> UserEvent:
    message = raw.get("message") or {}
    text = _flatten_text(message.get("content"))
    return UserEvent(
        uuid=str(raw.get("uuid", "")),
        timestamp=str(raw.get("timestamp", "")),
        text=text,
        is_sidechain=bool(raw.get("isSidechain", False)),
        is_api_error=bool(raw.get("isApiErrorMessage", False)),
        is_bot_prefixed=text.startswith(BOT_PREFIX),
    )


def _parse_assistant(raw: dict[str, Any]) -> AssistantEvent:
    message = raw.get("message") or {}
    content = message.get("content")
    text = _flatten_text(content)
    has_tool_use = _content_has_tool_use(content)
    usage = _parse_usage(message.get("usage"))
    return AssistantEvent(
        uuid=str(raw.get("uuid", "")),
        timestamp=str(raw.get("timestamp", "")),
        text=text,
        usage=usage,
        has_tool_use=has_tool_use,
        is_sidechain=bool(raw.get("isSidechain", False)),
        is_api_error=bool(raw.get("isApiErrorMessage", False)),
    )


def _parse_error(raw: dict[str, Any]) -> TranscriptEvent:
    # Claude Code currently writes max-limit events as top-level
    # ``{type:"error", subtype:"usage_limit_reached", ...}``. Other
    # error subtypes fall through to UnknownEvent for now.
    subtype = raw.get("subtype")
    if subtype == "usage_limit_reached":
        return UsageLimitEvent(
            uuid=str(raw.get("uuid", "")),
            timestamp=str(raw.get("timestamp", "")),
            reset_at=_opt_str(raw.get("reset_at") or raw.get("resetAt")),
            limit_kind=_opt_str(raw.get("limit_kind") or raw.get("kind")),
        )
    return UnknownEvent(type="error", raw=raw)


def _parse_system(raw: dict[str, Any]) -> SystemEvent:
    return SystemEvent(
        uuid=str(raw.get("uuid", "")),
        timestamp=str(raw.get("timestamp", "")),
        subtype=str(raw.get("subtype", "")),
        is_sidechain=bool(raw.get("isSidechain", False)),
    )


def _parse_usage(raw: Any) -> TokenUsage:
    if not isinstance(raw, dict):
        return TokenUsage()
    return TokenUsage(
        input_tokens=_coerce_int(raw.get("input_tokens")),
        output_tokens=_coerce_int(raw.get("output_tokens")),
        cache_creation_input_tokens=_coerce_int(
            raw.get("cache_creation_input_tokens")
        ),
        cache_read_input_tokens=_coerce_int(raw.get("cache_read_input_tokens")),
    )


def _flatten_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                txt = block.get("text")
                if isinstance(txt, str):
                    parts.append(txt)
        return "".join(parts)
    return ""


def _content_has_tool_use(content: Any) -> bool:
    if not isinstance(content, list):
        return False
    return any(
        isinstance(b, dict) and b.get("type") == "tool_use" for b in content
    )


def _coerce_int(value: Any) -> int:
    if isinstance(value, bool):  # bool is an int subclass — block it
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


# ---- High-level helpers ----------------------------------------------
#
# These operate over sequences of already-parsed events. The ingest
# service builds them incrementally from a streaming file; tests
# replay a full fixture list.


def main_chain(events: Sequence[TranscriptEvent]) -> list[TranscriptEvent]:
    """Drop sidechain + API-error events so callers can reason only
    about the primary conversation.

    ``SystemEvent`` and ``UnknownEvent`` don't have error flags but
    can be sidechain (subagent system messages) — they get filtered
    the same way.
    """
    out: list[TranscriptEvent] = []
    for ev in events:
        if isinstance(ev, UserEvent | AssistantEvent) and (
            ev.is_sidechain or ev.is_api_error
        ):
            continue
        if isinstance(ev, SystemEvent) and ev.is_sidechain:
            continue
        out.append(ev)
    return out


def should_emit_turn_end(events: Sequence[TranscriptEvent]) -> bool:
    """True iff the most recent relevant event signals Claude is done.

    "Relevant" means the main-chain events — sidechain subagent
    chatter is excluded. The rule (Spec §7) is: the last event is an
    ``assistant`` event *and* that event has no ``tool_use`` blocks.
    An assistant event with ``tool_use`` means Claude will follow up
    with a ``tool_result`` + another assistant turn; we wait for
    that one instead.
    """
    filtered = main_chain(events)
    for ev in reversed(filtered):
        if isinstance(ev, AssistantEvent):
            return not ev.has_tool_use
        if isinstance(ev, UserEvent):
            # A human user turn between Claude turns means we're
            # still mid-dialogue from the watcher's perspective.
            return False
    return False


def aggregate_tokens(events: Sequence[TranscriptEvent]) -> int:
    """Sum ``usage.total`` across all main-chain assistant events.

    Claude reports cumulative usage *on each turn* rather than
    incremental, so normally the token total is the ``usage.total``
    of the last assistant event. We take the max across the list to
    survive out-of-order tail reads and any event stream that
    ever resets mid-session.
    """
    best = 0
    for ev in main_chain(events):
        if isinstance(ev, AssistantEvent):
            best = max(best, ev.usage.total)
    return best
