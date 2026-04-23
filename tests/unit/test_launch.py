"""Unit tests for whatsbot.domain.launch — pure argv builder."""

from __future__ import annotations

import pytest

from whatsbot.domain.launch import build_claude_argv, render_command_line
from whatsbot.domain.projects import Mode

pytestmark = pytest.mark.unit


class TestBuildClaudeArgv:
    def test_fresh_session_has_no_resume(self) -> None:
        argv = build_claude_argv(
            safe_claude_binary="safe-claude", session_id="", mode=Mode.NORMAL
        )
        assert argv == ("safe-claude",)

    def test_resume_splices_id(self) -> None:
        argv = build_claude_argv(
            safe_claude_binary="safe-claude",
            session_id="abc-123",
            mode=Mode.NORMAL,
        )
        assert argv == ("safe-claude", "--resume", "abc-123")

    def test_strict_adds_permission_flag(self) -> None:
        argv = build_claude_argv(
            safe_claude_binary="safe-claude",
            session_id="abc-123",
            mode=Mode.STRICT,
        )
        assert argv == (
            "safe-claude",
            "--resume",
            "abc-123",
            "--permission-mode",
            "dontAsk",
        )

    def test_yolo_adds_dangerous_flag(self) -> None:
        argv = build_claude_argv(
            safe_claude_binary="safe-claude",
            session_id="",
            mode=Mode.YOLO,
        )
        assert argv == ("safe-claude", "--dangerously-skip-permissions")

    def test_binary_path_is_passed_through(self) -> None:
        argv = build_claude_argv(
            safe_claude_binary="/usr/local/bin/safe-claude",
            session_id="",
            mode=Mode.NORMAL,
        )
        assert argv[0] == "/usr/local/bin/safe-claude"


class TestRenderCommandLine:
    def test_simple_argv(self) -> None:
        line = render_command_line(("safe-claude", "--resume", "abc-123"))
        assert line == "safe-claude --resume abc-123"

    def test_quotes_binary_with_space(self) -> None:
        line = render_command_line(("/tmp/my tool/safe-claude",))
        # shlex.quote wraps the whole arg in single quotes when it
        # contains whitespace.
        assert "'" in line
        assert "/tmp/my tool/safe-claude" in line

    def test_quotes_dangerous_metacharacters(self) -> None:
        # Session IDs shouldn't carry $ or ; in practice, but the
        # render layer must still be safe against them.
        line = render_command_line(("safe-claude", "--resume", "id; rm -rf /"))
        assert "; rm -rf /" not in line.split("'id")[0]

    def test_empty_argv_raises(self) -> None:
        with pytest.raises(ValueError):
            render_command_line(())
