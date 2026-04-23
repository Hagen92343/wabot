"""Unit tests for whatsbot.domain.transcript.

Every fixture is a dict that gets ``json.dumps``-ed into a line the
way Claude Code actually writes transcripts. Keeping the inputs as
inline dicts (instead of on-disk JSONL fixtures) makes the schema
expectations easy to eyeball.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from whatsbot.domain.transcript import (
    BOT_PREFIX,
    AssistantEvent,
    SystemEvent,
    TokenUsage,
    TranscriptEvent,
    UnknownEvent,
    UsageLimitEvent,
    UserEvent,
    aggregate_tokens,
    main_chain,
    parse_line,
    should_emit_turn_end,
)

pytestmark = pytest.mark.unit


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


# ---- Malformed + empty inputs ----------------------------------------


def test_empty_line_returns_none() -> None:
    assert parse_line("") is None
    assert parse_line("   \n") is None


def test_malformed_json_returns_none() -> None:
    assert parse_line("{not-json") is None
    assert parse_line("42") is None  # JSON primitive, not a dict


def test_missing_type_returns_none() -> None:
    assert parse_line(_line({"foo": "bar"})) is None


def test_unknown_type_returns_unknown_event() -> None:
    ev = parse_line(_line({"type": "permission-mode", "mode": "bypass"}))
    assert isinstance(ev, UnknownEvent)
    assert ev.type == "permission-mode"
    assert ev.raw["mode"] == "bypass"


# ---- user events -----------------------------------------------------


def test_user_event_with_string_content() -> None:
    ev = parse_line(
        _line(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "2026-04-22T00:00:00Z",
                "message": {"content": "hi Claude"},
            }
        )
    )
    assert isinstance(ev, UserEvent)
    assert ev.text == "hi Claude"
    assert ev.is_bot_prefixed is False
    assert ev.is_sidechain is False


def test_user_event_with_list_content_concatenates_text_blocks() -> None:
    ev = parse_line(
        _line(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "t",
                "message": {
                    "content": [
                        {"type": "text", "text": "one "},
                        {"type": "tool_result", "content": "dropped"},
                        {"type": "text", "text": "two"},
                    ]
                },
            }
        )
    )
    assert isinstance(ev, UserEvent)
    assert ev.text == "one two"


def test_user_event_with_bot_prefix_detected() -> None:
    ev = parse_line(
        _line(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "t",
                "message": {"content": f"{BOT_PREFIX}prompt from bot"},
            }
        )
    )
    assert isinstance(ev, UserEvent)
    assert ev.is_bot_prefixed is True
    assert ev.text.startswith(BOT_PREFIX)


def test_user_event_carries_sidechain_and_api_error_flags() -> None:
    ev = parse_line(
        _line(
            {
                "type": "user",
                "uuid": "u1",
                "timestamp": "t",
                "isSidechain": True,
                "isApiErrorMessage": True,
                "message": {"content": "hi"},
            }
        )
    )
    assert isinstance(ev, UserEvent)
    assert ev.is_sidechain is True
    assert ev.is_api_error is True


# ---- assistant events + usage ---------------------------------------


def test_assistant_event_extracts_text_ignores_thinking() -> None:
    ev = parse_line(
        _line(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "t",
                "message": {
                    "content": [
                        {"type": "thinking", "thinking": "hidden"},
                        {"type": "text", "text": "visible response"},
                    ],
                    "usage": {},
                },
            }
        )
    )
    assert isinstance(ev, AssistantEvent)
    assert ev.text == "visible response"
    assert ev.has_tool_use is False


def test_assistant_event_with_tool_use_flag() -> None:
    ev = parse_line(
        _line(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "t",
                "message": {
                    "content": [
                        {"type": "text", "text": "calling a tool"},
                        {"type": "tool_use", "id": "t1", "name": "Bash"},
                    ],
                    "usage": {},
                },
            }
        )
    )
    assert isinstance(ev, AssistantEvent)
    assert ev.has_tool_use is True


def test_assistant_event_token_usage_extracted() -> None:
    ev = parse_line(
        _line(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "t",
                "message": {
                    "content": [{"type": "text", "text": "hi"}],
                    "usage": {
                        "input_tokens": 100,
                        "output_tokens": 20,
                        "cache_creation_input_tokens": 500,
                        "cache_read_input_tokens": 10,
                        "server_tool_use": "ignored",
                    },
                },
            }
        )
    )
    assert isinstance(ev, AssistantEvent)
    assert ev.usage == TokenUsage(
        input_tokens=100,
        output_tokens=20,
        cache_creation_input_tokens=500,
        cache_read_input_tokens=10,
    )
    assert ev.usage.total == 630


def test_assistant_event_missing_usage_defaults_to_zero() -> None:
    ev = parse_line(
        _line(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "t",
                "message": {"content": []},
            }
        )
    )
    assert isinstance(ev, AssistantEvent)
    assert ev.usage.total == 0


def test_assistant_event_with_non_int_usage_values_coerces_to_zero() -> None:
    ev = parse_line(
        _line(
            {
                "type": "assistant",
                "uuid": "a1",
                "timestamp": "t",
                "message": {
                    "content": [],
                    "usage": {
                        "input_tokens": "not-a-number",
                        "output_tokens": True,  # bool must not become 1
                        "cache_creation_input_tokens": None,
                        "cache_read_input_tokens": 3.9,
                    },
                },
            }
        )
    )
    assert isinstance(ev, AssistantEvent)
    assert ev.usage.input_tokens == 0
    assert ev.usage.output_tokens == 0
    assert ev.usage.cache_creation_input_tokens == 0
    # floats are truncated
    assert ev.usage.cache_read_input_tokens == 3


# ---- error / usage-limit / system -----------------------------------


def test_usage_limit_event_parsed() -> None:
    ev = parse_line(
        _line(
            {
                "type": "error",
                "subtype": "usage_limit_reached",
                "uuid": "e1",
                "timestamp": "t",
                "reset_at": "2026-04-22T15:00:00Z",
                "limit_kind": "session_5h",
            }
        )
    )
    assert isinstance(ev, UsageLimitEvent)
    assert ev.reset_at == "2026-04-22T15:00:00Z"
    assert ev.limit_kind == "session_5h"


def test_error_event_other_subtype_falls_through_to_unknown() -> None:
    ev = parse_line(
        _line(
            {
                "type": "error",
                "subtype": "something-else",
                "message": "oops",
            }
        )
    )
    assert isinstance(ev, UnknownEvent)
    assert ev.type == "error"


def test_system_event_carries_subtype() -> None:
    ev = parse_line(
        _line(
            {
                "type": "system",
                "uuid": "s1",
                "timestamp": "t",
                "subtype": "stop_hook",
            }
        )
    )
    assert isinstance(ev, SystemEvent)
    assert ev.subtype == "stop_hook"


# ---- main_chain filter ----------------------------------------------


def test_main_chain_drops_sidechain_and_api_error_events() -> None:
    events: list[TranscriptEvent] = [
        UserEvent(uuid="u1", timestamp="t", text="hi"),
        UserEvent(uuid="u2", timestamp="t", text="sub", is_sidechain=True),
        AssistantEvent(
            uuid="a1",
            timestamp="t",
            text="pong",
            usage=TokenUsage(output_tokens=5),
            has_tool_use=False,
        ),
        AssistantEvent(
            uuid="a2",
            timestamp="t",
            text="err",
            usage=TokenUsage(),
            has_tool_use=False,
            is_api_error=True,
        ),
        SystemEvent(uuid="s1", timestamp="t", subtype="stop", is_sidechain=True),
        SystemEvent(uuid="s2", timestamp="t", subtype="other"),
    ]
    result = main_chain(events)
    assert len(result) == 3
    kept = [e for e in result if not isinstance(e, UnknownEvent)]
    assert [e.uuid for e in kept] == ["u1", "a1", "s2"]


# ---- turn-end detection ---------------------------------------------


def test_turn_end_true_after_plain_assistant_event() -> None:
    events: list[TranscriptEvent] = [
        UserEvent(uuid="u1", timestamp="t", text="hi"),
        AssistantEvent(
            uuid="a1",
            timestamp="t",
            text="pong",
            usage=TokenUsage(output_tokens=5),
            has_tool_use=False,
        ),
    ]
    assert should_emit_turn_end(events) is True


def test_turn_end_false_when_last_assistant_has_tool_use() -> None:
    events: list[TranscriptEvent] = [
        UserEvent(uuid="u1", timestamp="t", text="hi"),
        AssistantEvent(
            uuid="a1",
            timestamp="t",
            text="let me check",
            usage=TokenUsage(),
            has_tool_use=True,
        ),
    ]
    assert should_emit_turn_end(events) is False


def test_turn_end_false_when_user_is_last() -> None:
    events: list[TranscriptEvent] = [UserEvent(uuid="u1", timestamp="t", text="hi")]
    assert should_emit_turn_end(events) is False


def test_turn_end_ignores_trailing_sidechain_chatter() -> None:
    events: list[TranscriptEvent] = [
        UserEvent(uuid="u1", timestamp="t", text="hi"),
        AssistantEvent(
            uuid="a1",
            timestamp="t",
            text="done",
            usage=TokenUsage(),
            has_tool_use=False,
        ),
        AssistantEvent(
            uuid="a2",
            timestamp="t",
            text="subagent",
            usage=TokenUsage(),
            has_tool_use=False,
            is_sidechain=True,
        ),
    ]
    assert should_emit_turn_end(events) is True


def test_turn_end_false_on_empty_event_list() -> None:
    assert should_emit_turn_end([]) is False


# ---- token aggregation ----------------------------------------------


def test_aggregate_tokens_picks_max_across_main_chain() -> None:
    events: list[TranscriptEvent] = [
        AssistantEvent(
            uuid="a1",
            timestamp="t",
            text="",
            usage=TokenUsage(input_tokens=100, output_tokens=10),
            has_tool_use=False,
        ),
        AssistantEvent(
            uuid="a2",
            timestamp="t",
            text="",
            usage=TokenUsage(input_tokens=200, output_tokens=15),
            has_tool_use=False,
        ),
    ]
    assert aggregate_tokens(events) == 215


def test_aggregate_tokens_ignores_sidechain_events() -> None:
    events: list[TranscriptEvent] = [
        AssistantEvent(
            uuid="a1",
            timestamp="t",
            text="",
            usage=TokenUsage(input_tokens=100),
            has_tool_use=False,
        ),
        AssistantEvent(
            uuid="a2",
            timestamp="t",
            text="",
            usage=TokenUsage(input_tokens=999_999),
            has_tool_use=False,
            is_sidechain=True,
        ),
    ]
    assert aggregate_tokens(events) == 100


def test_aggregate_tokens_empty_list_is_zero() -> None:
    assert aggregate_tokens([]) == 0


# ---- real transcript shape sanity -----------------------------------


def test_handles_real_claude_assistant_shape() -> None:
    """Sanity check against the exact key set observed in a live
    ``~/.claude/projects/.../*.jsonl`` — the parser must ignore the
    extra top-level bookkeeping fields (parentUuid, requestId,
    version, gitBranch, ...) without losing the message content."""
    line = _line(
        {
            "parentUuid": "x",
            "isSidechain": False,
            "message": {
                "model": "claude-opus-4-7",
                "id": "msg_...",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "..."},
                    {"type": "text", "text": "Hello!"},
                ],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 7,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": 100,
                    "output_tokens": 3,
                    "service_tier": "priority",
                },
            },
            "requestId": "req_...",
            "type": "assistant",
            "uuid": "abc-123",
            "timestamp": "2026-04-22T15:27:09.123Z",
            "version": "2.1.117",
            "gitBranch": "main",
        }
    )
    ev = parse_line(line)
    assert isinstance(ev, AssistantEvent)
    assert ev.text == "Hello!"
    assert ev.has_tool_use is False
    assert ev.usage.total == 110
