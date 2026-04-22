"""Unit tests for whatsbot.domain.modes."""

from __future__ import annotations

import pytest

from whatsbot.domain.modes import (
    claude_flags,
    mode_badge,
    status_bar_color,
    valid_transition,
)
from whatsbot.domain.projects import Mode

pytestmark = pytest.mark.unit


class TestClaudeFlags:
    def test_normal_has_no_flags(self) -> None:
        # Default permission mode — no CLI override.
        assert claude_flags(Mode.NORMAL) == ()

    def test_strict_emits_permission_mode_dontask(self) -> None:
        assert claude_flags(Mode.STRICT) == ("--permission-mode", "dontAsk")

    def test_yolo_emits_dangerously_skip(self) -> None:
        assert claude_flags(Mode.YOLO) == ("--dangerously-skip-permissions",)

    def test_flags_are_tuples_not_lists(self) -> None:
        # Callers splat with ``*claude_flags(mode)`` into argv. Returning
        # an immutable tuple makes accidental mutation impossible.
        for mode in Mode:
            assert isinstance(claude_flags(mode), tuple)


class TestStatusBarColor:
    @pytest.mark.parametrize(
        "mode,color",
        [
            (Mode.NORMAL, "green"),
            (Mode.STRICT, "blue"),
            (Mode.YOLO, "red"),
        ],
    )
    def test_each_mode_maps(self, mode: Mode, color: str) -> None:
        assert status_bar_color(mode) == color


class TestModeBadge:
    def test_badge_contains_mode_name(self) -> None:
        assert "NORMAL" in mode_badge(Mode.NORMAL)
        assert "STRICT" in mode_badge(Mode.STRICT)
        assert "YOLO" in mode_badge(Mode.YOLO)


class TestValidTransition:
    @pytest.mark.parametrize("from_mode", list(Mode))
    @pytest.mark.parametrize("to_mode", list(Mode))
    def test_every_transition_is_currently_valid(
        self, from_mode: Mode, to_mode: Mode
    ) -> None:
        # Today all 9 combinations are allowed (incl. same-mode).
        # When a rule lands (e.g. "YOLO→Strict must go via Normal"),
        # the corresponding case here flips to ``False`` and we get a
        # failing test that documents the change.
        assert valid_transition(from_mode, to_mode) is True
