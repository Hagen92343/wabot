"""Unit tests for whatsbot.domain.smart_detection (C2.2 subset)."""

from __future__ import annotations

from pathlib import Path

import pytest

from whatsbot.domain.smart_detection import detect

pytestmark = pytest.mark.unit


def test_empty_dir_returns_no_artifacts(tmp_path: Path) -> None:
    result = detect(tmp_path)
    assert result.artifacts_found == []
    assert result.suggested_rules == []


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    """Defensive: a non-existent path must not crash."""
    result = detect(tmp_path / "ghost")
    assert result.artifacts_found == []
    assert result.suggested_rules == []


def test_package_json_yields_npm_rules(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    result = detect(tmp_path)
    assert "package.json" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert "npm test" in patterns
    assert "npm install" in patterns
    assert "npm ci" in patterns
    assert "npm run *" in patterns
    assert "npx *" in patterns
    # All package.json rules cite the right reason.
    for rule in result.suggested_rules:
        if rule.pattern.startswith("npm") or rule.pattern.startswith("npx"):
            assert "package.json" in rule.reason


def test_git_dir_yields_git_rules(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    result = detect(tmp_path)
    assert ".git" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert "git status" in patterns
    assert "git diff *" in patterns
    assert "git log *" in patterns
    assert "git branch *" in patterns
    assert "git fetch *" in patterns


def test_combined_artifacts_yield_union_of_rules(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    result = detect(tmp_path)
    assert set(result.artifacts_found) == {"package.json", ".git"}
    # 5 npm rules + 7 git rules = 12 total
    assert len(result.suggested_rules) == 12


def test_all_rules_target_bash_tool_in_phase_2_2(tmp_path: Path) -> None:
    """Phase 2.2 only emits Bash rules. Phase 2.3 may add Read/Edit suggestions."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    result = detect(tmp_path)
    assert {r.tool for r in result.suggested_rules} == {"Bash"}


def test_package_json_must_be_a_file_not_a_dir(tmp_path: Path) -> None:
    """Some hostile repo could have a directory named package.json — we
    must not treat that as an npm project."""
    (tmp_path / "package.json").mkdir()
    result = detect(tmp_path)
    assert "package.json" not in result.artifacts_found
