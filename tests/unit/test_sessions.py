"""Unit tests for whatsbot.domain.sessions (pure)."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from whatsbot.domain.projects import Mode
from whatsbot.domain.sessions import (
    AUTO_COMPACT_THRESHOLD,
    CLAUDE_CONTEXT_LIMIT,
    ClaudeSession,
    context_fill_ratio,
    should_auto_compact,
    tmux_session_name,
)

pytestmark = pytest.mark.unit


def _base_session(**kwargs: object) -> ClaudeSession:
    defaults: dict[str, object] = {
        "project_name": "alpha",
        "session_id": "sess-abc",
        "transcript_path": "/tmp/t.jsonl",
        "started_at": datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
    }
    defaults.update(kwargs)
    return ClaudeSession(**defaults)  # type: ignore[arg-type]


# ---- tmux_session_name ------------------------------------------------


def test_tmux_name_prefixes_wb_dash() -> None:
    assert tmux_session_name("website") == "wb-website"


def test_tmux_name_preserves_hyphens_and_underscores() -> None:
    assert tmux_session_name("my_app-v2") == "wb-my_app-v2"


# ---- context_fill_ratio ----------------------------------------------


class TestContextFillRatio:
    def test_zero_tokens_is_zero(self) -> None:
        assert context_fill_ratio(0) == 0.0

    def test_negative_clamps_to_zero(self) -> None:
        assert context_fill_ratio(-100) == 0.0

    def test_half_limit_is_half(self) -> None:
        assert (
            context_fill_ratio(CLAUDE_CONTEXT_LIMIT // 2)
            == pytest.approx(0.5)
        )

    def test_over_limit_clamps_to_one(self) -> None:
        assert context_fill_ratio(CLAUDE_CONTEXT_LIMIT * 2) == 1.0

    def test_custom_limit(self) -> None:
        assert context_fill_ratio(50, limit=100) == 0.5

    def test_zero_limit_raises(self) -> None:
        with pytest.raises(ValueError):
            context_fill_ratio(10, limit=0)


# ---- should_auto_compact ---------------------------------------------


class TestShouldAutoCompact:
    def test_under_threshold_is_false(self) -> None:
        s = _base_session(
            tokens_used=100_000, context_fill_ratio=0.5
        )
        assert should_auto_compact(s) is False

    def test_at_threshold_is_true(self) -> None:
        s = _base_session(
            tokens_used=160_000, context_fill_ratio=AUTO_COMPACT_THRESHOLD
        )
        assert should_auto_compact(s) is True

    def test_above_threshold_is_true(self) -> None:
        s = _base_session(tokens_used=190_000, context_fill_ratio=0.95)
        assert should_auto_compact(s) is True

    def test_zero_tokens_never_compacts(self) -> None:
        # Even if the ratio were somehow high, 0 tokens means we're
        # fresh — nothing to compact.
        s = _base_session(tokens_used=0, context_fill_ratio=1.0)
        assert should_auto_compact(s) is False

    def test_custom_threshold(self) -> None:
        s = _base_session(tokens_used=100_000, context_fill_ratio=0.5)
        assert should_auto_compact(s, threshold=0.4) is True


# ---- ClaudeSession helpers -------------------------------------------


class TestClaudeSessionHelpers:
    def test_with_tokens_updates_ratio(self) -> None:
        s = _base_session()
        updated = s.with_tokens(CLAUDE_CONTEXT_LIMIT // 4)
        assert updated.tokens_used == CLAUDE_CONTEXT_LIMIT // 4
        assert updated.context_fill_ratio == pytest.approx(0.25)

    def test_with_mode_returns_copy(self) -> None:
        s = _base_session(current_mode=Mode.NORMAL)
        flipped = s.with_mode(Mode.STRICT)
        assert flipped.current_mode is Mode.STRICT
        # Original stays untouched — frozen dataclass.
        assert s.current_mode is Mode.NORMAL

    def test_mark_compact_resets_tokens(self) -> None:
        s = _base_session(tokens_used=180_000, context_fill_ratio=0.9)
        now = datetime(2026, 4, 22, 13, 0, tzinfo=UTC)
        compacted = s.mark_compact(now)
        assert compacted.last_compact_at == now
        assert compacted.tokens_used == 0
        assert compacted.context_fill_ratio == 0.0

    def test_is_frozen(self) -> None:
        from dataclasses import FrozenInstanceError

        s = _base_session()
        with pytest.raises(FrozenInstanceError):
            s.tokens_used = 999  # type: ignore[misc]
