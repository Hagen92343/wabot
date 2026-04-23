"""Unit tests for whatsbot.application.transcript_ingest."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import pytest

from whatsbot.application.transcript_ingest import TranscriptIngest
from whatsbot.domain.projects import Mode
from whatsbot.domain.transcript import BOT_PREFIX, UsageLimitEvent

pytestmark = pytest.mark.unit


# ---- fakes ----------------------------------------------------------


@dataclass
class FakeSessionRepo:
    """Records update_activity calls; other methods are not exercised."""

    activity_calls: list[tuple[str, int]] = field(default_factory=list)
    raise_on_update: Exception | None = None

    def upsert(self, session: Any) -> None:  # pragma: no cover
        raise NotImplementedError

    def get(self, project_name: str) -> Any:  # pragma: no cover
        raise NotImplementedError

    def list_all(self) -> list[Any]:  # pragma: no cover
        return []

    def delete(self, project_name: str) -> bool:  # pragma: no cover
        return False

    def update_activity(
        self,
        project_name: str,
        *,
        tokens_used: int,
        last_activity_at: datetime,
    ) -> None:
        del last_activity_at
        if self.raise_on_update is not None:
            raise self.raise_on_update
        self.activity_calls.append((project_name, tokens_used))

    def bump_turn(self, project_name: str, *, at: datetime) -> None:  # pragma: no cover
        pass

    def update_mode(self, project_name: str, mode: Mode) -> None:  # pragma: no cover
        pass

    def mark_compact(self, project_name: str, at: datetime) -> None:  # pragma: no cover
        pass


@dataclass
class Recorder:
    turn_ends: list[tuple[str, str]] = field(default_factory=list)
    usage_limits: list[tuple[str, UsageLimitEvent]] = field(default_factory=list)

    def on_turn_end(self, project: str, text: str) -> None:
        self.turn_ends.append((project, text))

    def on_usage_limit(self, project: str, event: UsageLimitEvent) -> None:
        self.usage_limits.append((project, event))


def _ingest(
    *, repo: FakeSessionRepo | None = None, with_limits: bool = False
) -> tuple[TranscriptIngest, FakeSessionRepo, Recorder]:
    actual_repo = repo or FakeSessionRepo()
    recorder = Recorder()
    ingest = TranscriptIngest(
        session_repo=actual_repo,
        on_turn_end=recorder.on_turn_end,
        on_usage_limit=recorder.on_usage_limit if with_limits else None,
    )
    return ingest, actual_repo, recorder


def _line(payload: dict[str, Any]) -> str:
    return json.dumps(payload)


def _user(text: str, *, sidechain: bool = False, api_error: bool = False) -> str:
    return _line(
        {
            "type": "user",
            "uuid": f"u-{hash(text) & 0xFFFF:x}",
            "timestamp": "t",
            "isSidechain": sidechain,
            "isApiErrorMessage": api_error,
            "message": {"content": text},
        }
    )


def _assistant(
    text: str,
    *,
    has_tool_use: bool = False,
    tokens: int = 0,
    sidechain: bool = False,
) -> str:
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    if has_tool_use:
        content.append({"type": "tool_use", "id": "t1", "name": "Bash"})
    usage = {"input_tokens": tokens, "output_tokens": 0}
    return _line(
        {
            "type": "assistant",
            "uuid": f"a-{hash(text) & 0xFFFF:x}",
            "timestamp": "t",
            "isSidechain": sidechain,
            "message": {"content": content, "usage": usage},
        }
    )


# ---- single-turn flow -----------------------------------------------


def test_single_turn_fires_on_assistant_without_tool_use() -> None:
    ingest, repo, recorder = _ingest()
    ingest.feed("alpha", _user(f"{BOT_PREFIX}hi"))
    ingest.feed("alpha", _assistant("hello from Claude", tokens=100))

    assert recorder.turn_ends == [("alpha", "hello from Claude")]
    assert repo.activity_calls == [("alpha", 100)]


def test_assistant_with_tool_use_does_not_fire() -> None:
    ingest, _, recorder = _ingest()
    ingest.feed("alpha", _user(f"{BOT_PREFIX}do X"))
    ingest.feed("alpha", _assistant("let me check", has_tool_use=True))
    # Simulate a later tool_result + another assistant, now without
    # tool_use. Only the last assistant closes the turn.
    ingest.feed(
        "alpha",
        _line(
            {
                "type": "user",
                "uuid": "u2",
                "timestamp": "t",
                "message": {
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "t1",
                            "content": "ran",
                        }
                    ]
                },
            }
        ),
    )
    assert recorder.turn_ends == []
    ingest.feed("alpha", _assistant("done", tokens=200))
    assert len(recorder.turn_ends) == 1
    project, text = recorder.turn_ends[0]
    assert project == "alpha"
    # Assembly: both assistant texts should be in the turn-end body
    # so the user sees the full narrative ("let me check\ndone").
    assert "let me check" in text
    assert "done" in text


# ---- filtering ------------------------------------------------------


def test_sidechain_events_are_ignored() -> None:
    ingest, repo, recorder = _ingest()
    ingest.feed("alpha", _user("sub-agent input", sidechain=True))
    ingest.feed(
        "alpha", _assistant("sub-agent output", sidechain=True, tokens=9999)
    )
    assert recorder.turn_ends == []
    assert repo.activity_calls == []


def test_api_error_user_events_are_ignored() -> None:
    ingest, repo, recorder = _ingest()
    ingest.feed("alpha", _user("bogus", api_error=True))
    ingest.feed("alpha", _assistant("should still fire", tokens=10))
    # The assistant event is not filtered; it fires a turn end as
    # usual. What must NOT happen: the api-error user event clears
    # the buffer mid-turn. Coverage is the single turn_end here.
    assert recorder.turn_ends == [("alpha", "should still fire")]


def test_malformed_lines_are_silently_dropped() -> None:
    ingest, repo, recorder = _ingest()
    ingest.feed("alpha", "{not-json")
    ingest.feed("alpha", "")
    ingest.feed("alpha", _line({"type": "permission-mode", "mode": "bypass"}))
    assert recorder.turn_ends == []
    assert repo.activity_calls == []


# ---- user-turn buffer reset -----------------------------------------


def test_human_user_turn_clears_pending_assistant_buffer() -> None:
    """If Claude was mid-turn (emitted partial assistant text with
    tool_use) and then a *human* user turn arrives at the local
    terminal, the bot-side buffer must be dropped — the human took
    over, the response is no longer WhatsApp's to announce."""
    ingest, _, recorder = _ingest()
    ingest.feed("alpha", _assistant("half-done", has_tool_use=True))
    # Human typed at the terminal — no ZWSP prefix.
    ingest.feed("alpha", _user("nevermind, I'll do it myself"))
    # Now the session finishes later. The buffer was reset, so the
    # turn end should contain only the post-reset assistant text.
    ingest.feed("alpha", _assistant("ok then", tokens=50))
    assert recorder.turn_ends == [("alpha", "ok then")]


def test_bot_prefixed_user_turn_does_not_reset_buffer() -> None:
    ingest, _, recorder = _ingest()
    ingest.feed("alpha", _assistant("thinking out loud", has_tool_use=True))
    # Bot-originated user turn (ZWSP prefix) — same conversation.
    ingest.feed("alpha", _user(f"{BOT_PREFIX}continue please"))
    ingest.feed("alpha", _assistant("final", tokens=25))
    assert len(recorder.turn_ends) == 1
    text = recorder.turn_ends[0][1]
    # Both assistant texts are in the final delivery.
    assert "thinking out loud" in text
    assert "final" in text


# ---- multi-project isolation ---------------------------------------


def test_two_projects_do_not_share_state() -> None:
    ingest, _, recorder = _ingest()
    ingest.feed("alpha", _assistant("alpha-turn", tokens=10))
    ingest.feed("beta", _assistant("beta-turn", tokens=20))
    assert set(recorder.turn_ends) == {
        ("alpha", "alpha-turn"),
        ("beta", "beta-turn"),
    }


# ---- token persistence ---------------------------------------------


def test_token_totals_track_max_and_persist_on_each_increase() -> None:
    ingest, repo, _ = _ingest()
    ingest.feed("alpha", _assistant("one", has_tool_use=True, tokens=100))
    ingest.feed("alpha", _assistant("two", tokens=250))
    # Tokens persisted at every higher reading.
    assert repo.activity_calls == [("alpha", 100), ("alpha", 250)]


def test_token_persist_not_repeated_when_count_unchanged() -> None:
    ingest, repo, _ = _ingest()
    ingest.feed("alpha", _assistant("one", has_tool_use=True, tokens=150))
    ingest.feed("alpha", _assistant("two", tokens=150))  # same total
    assert repo.activity_calls == [("alpha", 150)]


def test_db_error_does_not_kill_ingest_thread() -> None:
    repo = FakeSessionRepo(raise_on_update=RuntimeError("db down"))
    ingest, _, recorder = _ingest(repo=repo)
    # The exception must be swallowed so the observer keeps running.
    ingest.feed("alpha", _assistant("hi", tokens=10))
    # Turn end still fires (persistence failure is logged, not
    # propagated).
    assert recorder.turn_ends == [("alpha", "hi")]


# ---- usage-limit callback -------------------------------------------


def test_usage_limit_forwards_to_callback_when_configured() -> None:
    ingest, _, recorder = _ingest(with_limits=True)
    ingest.feed(
        "alpha",
        _line(
            {
                "type": "error",
                "subtype": "usage_limit_reached",
                "uuid": "e1",
                "timestamp": "t",
                "reset_at": "2026-04-22T15:00:00Z",
                "limit_kind": "session_5h",
            }
        ),
    )
    assert len(recorder.usage_limits) == 1
    project, event = recorder.usage_limits[0]
    assert project == "alpha"
    assert event.reset_at == "2026-04-22T15:00:00Z"


def test_usage_limit_logs_even_without_callback() -> None:
    ingest, _, _ = _ingest(with_limits=False)
    # Must not raise — the absence of a callback just means the
    # event is logged and dropped.
    ingest.feed(
        "alpha",
        _line(
            {
                "type": "error",
                "subtype": "usage_limit_reached",
                "uuid": "e1",
                "timestamp": "t",
            }
        ),
    )


# ---- reset ----------------------------------------------------------


def test_reset_drops_in_flight_turn_state() -> None:
    ingest, _, recorder = _ingest()
    ingest.feed("alpha", _assistant("mid-turn", has_tool_use=True))
    ingest.reset("alpha")
    # Subsequent assistant without tool_use fires a turn end with
    # only post-reset content.
    ingest.feed("alpha", _assistant("after reset", tokens=10))
    assert recorder.turn_ends == [("alpha", "after reset")]


def test_reset_on_unknown_project_is_noop() -> None:
    ingest, _, _ = _ingest()
    # Must not raise.
    ingest.reset("ghost")
