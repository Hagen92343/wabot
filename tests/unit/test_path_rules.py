"""Unit tests for whatsbot.domain.path_rules (Spec §12 Layer 3)."""

from __future__ import annotations

from pathlib import Path

import pytest

from whatsbot.domain.hook_decisions import Verdict
from whatsbot.domain.path_rules import (
    PathCategory,
    classify_path,
    evaluate_write,
)
from whatsbot.domain.projects import Mode

pytestmark = pytest.mark.unit


# ---- classify_path -----------------------------------------------------


class TestClassifyPath:
    def test_project_scope_match(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / "src" / "main.py"
        assert (
            classify_path(target, project_cwd=project) is PathCategory.PROJECT
        )

    def test_temp_scope_slash_tmp(self) -> None:
        assert (
            classify_path(Path("/tmp/foo.txt"), project_cwd=None)
            is PathCategory.TEMP
        )

    def test_temp_scope_private_tmp(self) -> None:
        assert (
            classify_path(Path("/private/tmp/bar.log"), project_cwd=None)
            is PathCategory.TEMP
        )

    def test_protected_git(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / ".git" / "config"
        # Even inside project_cwd, .git is protected.
        assert (
            classify_path(target, project_cwd=project)
            is PathCategory.PROTECTED
        )

    def test_protected_vscode_anywhere(self) -> None:
        assert (
            classify_path(Path("/anywhere/.vscode/settings.json"), project_cwd=None)
            is PathCategory.PROTECTED
        )

    def test_protected_idea_anywhere(self) -> None:
        assert (
            classify_path(Path("/anywhere/.idea/workspace.xml"), project_cwd=None)
            is PathCategory.PROTECTED
        )

    def test_protected_claude_root_level(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / ".claude" / "settings.json"
        assert (
            classify_path(target, project_cwd=project)
            is PathCategory.PROTECTED
        )

    def test_claude_commands_is_allowed(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / ".claude" / "commands" / "hello.md"
        # .claude/commands is the user-customisation exception.
        assert (
            classify_path(target, project_cwd=project) is PathCategory.PROJECT
        )

    def test_claude_agents_is_allowed(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / ".claude" / "agents" / "foo.md"
        assert (
            classify_path(target, project_cwd=project) is PathCategory.PROJECT
        )

    def test_claude_skills_is_allowed(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / ".claude" / "skills" / "foo.md"
        assert (
            classify_path(target, project_cwd=project) is PathCategory.PROJECT
        )

    def test_other_path_outside_project_and_temp(self) -> None:
        assert (
            classify_path(Path("/etc/hosts"), project_cwd=None)
            is PathCategory.OTHER
        )

    def test_protected_wins_over_project_scope_even_under_claude_exception(
        self, tmp_path: Path
    ) -> None:
        """.claude/commands is allowed, but a .git nested deeper
        reasserts protection."""
        project = tmp_path / "alpha"
        target = project / ".claude" / "commands" / ".git" / "hooks"
        # The later .git segment pulls the whole path back into
        # PROTECTED — even though .claude/commands started down the
        # sanctioned path.
        assert (
            classify_path(target, project_cwd=project)
            is PathCategory.PROTECTED
        )

    def test_project_cwd_none_means_no_project_scope(self) -> None:
        result = classify_path(Path("/some/random/file"), project_cwd=None)
        assert result is PathCategory.OTHER


# ---- evaluate_write ---------------------------------------------------


class TestEvaluateWrite:
    def test_protected_denies_in_every_mode(self, tmp_path: Path) -> None:
        target = tmp_path / ".git" / "config"
        for mode in (Mode.NORMAL, Mode.STRICT, Mode.YOLO):
            d = evaluate_write(target, project_cwd=tmp_path, mode=mode)
            assert d.verdict is Verdict.DENY
            assert "protected path" in d.reason.lower()

    def test_project_allows_in_every_mode(self, tmp_path: Path) -> None:
        project = tmp_path / "alpha"
        target = project / "src" / "main.py"
        for mode in (Mode.NORMAL, Mode.STRICT, Mode.YOLO):
            d = evaluate_write(target, project_cwd=project, mode=mode)
            assert d.verdict is Verdict.ALLOW

    def test_temp_allows_in_every_mode(self) -> None:
        for mode in (Mode.NORMAL, Mode.STRICT, Mode.YOLO):
            d = evaluate_write(
                Path("/tmp/foo"), project_cwd=None, mode=mode
            )
            assert d.verdict is Verdict.ALLOW

    def test_other_path_normal_asks_user(self) -> None:
        d = evaluate_write(
            Path("/etc/hosts"), project_cwd=None, mode=Mode.NORMAL
        )
        assert d.verdict is Verdict.ASK_USER

    def test_other_path_strict_denies(self) -> None:
        d = evaluate_write(
            Path("/etc/hosts"), project_cwd=None, mode=Mode.STRICT
        )
        assert d.verdict is Verdict.DENY
        assert "strict" in d.reason.lower()

    def test_other_path_yolo_allows(self) -> None:
        d = evaluate_write(
            Path("/etc/hosts"), project_cwd=None, mode=Mode.YOLO
        )
        assert d.verdict is Verdict.ALLOW

    def test_claude_commands_allows_even_in_strict(
        self, tmp_path: Path
    ) -> None:
        """Spec §12 exception: .claude/commands is the user-customisation
        entrypoint — Strict mode must still permit writes here."""
        project = tmp_path / "alpha"
        target = project / ".claude" / "commands" / "deploy.md"
        d = evaluate_write(target, project_cwd=project, mode=Mode.STRICT)
        assert d.verdict is Verdict.ALLOW
