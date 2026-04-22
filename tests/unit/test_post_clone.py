"""Unit tests for whatsbot.application.post_clone."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whatsbot.application import post_clone
from whatsbot.domain.smart_detection import AllowRule, DetectionResult

pytestmark = pytest.mark.unit


# --- .claudeignore ---------------------------------------------------------


def test_write_claudeignore_includes_secret_patterns(tmp_path: Path) -> None:
    target = post_clone.write_claudeignore(tmp_path)
    text = target.read_text(encoding="utf-8")
    # Spec §12 Layer 5 — must catch the obvious secret stash patterns.
    for needle in (".env", "secrets/", "*.pem", "id_rsa*", "credentials.*", ".aws/"):
        assert needle in text


def test_write_claudeignore_blocks_whatsbot_internal(tmp_path: Path) -> None:
    """Don't let Claude read its own per-project bot files."""
    text = post_clone.write_claudeignore(tmp_path).read_text(encoding="utf-8")
    assert ".whatsbot/" in text


def test_write_claudeignore_overwrites(tmp_path: Path) -> None:
    (tmp_path / ".claudeignore").write_text("OLD CONTENT", encoding="utf-8")
    target = post_clone.write_claudeignore(tmp_path)
    assert "OLD CONTENT" not in target.read_text(encoding="utf-8")


# --- .whatsbot/config.json -------------------------------------------------


def test_write_config_json_creates_subdir_and_payload(tmp_path: Path) -> None:
    target = post_clone.write_config_json(
        tmp_path,
        project_name="alpha",
        source_url="https://github.com/o/r",
        source_mode="git",
    )
    assert target == tmp_path / ".whatsbot" / "config.json"
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["name"] == "alpha"
    assert payload["source_mode"] == "git"
    assert payload["source_url"] == "https://github.com/o/r"
    assert payload["schema_version"] == 1
    assert "created_at" in payload  # ISO timestamp


def test_write_config_json_handles_no_source_url(tmp_path: Path) -> None:
    target = post_clone.write_config_json(
        tmp_path, project_name="alpha", source_url=None, source_mode="empty"
    )
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["source_url"] is None
    assert payload["source_mode"] == "empty"


# --- CLAUDE.md -------------------------------------------------------------


def test_write_claude_md_creates_when_missing(tmp_path: Path) -> None:
    target = post_clone.write_claude_md_if_missing(tmp_path, project_name="alpha")
    assert target is not None
    text = target.read_text(encoding="utf-8")
    assert "alpha" in text  # template substitution
    assert "untrusted_content" in text  # injection-defense reminder
    assert "main" in text and "master" in text  # push-protection note


def test_write_claude_md_skips_when_present(tmp_path: Path) -> None:
    """Don't overwrite a CLAUDE.md the upstream repo shipped."""
    existing = tmp_path / "CLAUDE.md"
    existing.write_text("UPSTREAM CONTENT", encoding="utf-8")
    target = post_clone.write_claude_md_if_missing(tmp_path, project_name="alpha")
    assert target is None
    assert existing.read_text(encoding="utf-8") == "UPSTREAM CONTENT"


# --- suggested-rules.json --------------------------------------------------


def _detection(rules: int) -> DetectionResult:
    return DetectionResult(
        artifacts_found=["package.json", ".git"],
        suggested_rules=[
            AllowRule(tool="Bash", pattern=f"cmd-{i}", reason="test") for i in range(rules)
        ],
    )


def test_write_suggested_rules_writes_payload(tmp_path: Path) -> None:
    target = post_clone.write_suggested_rules(tmp_path, _detection(3))
    assert target is not None
    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["artifacts_found"] == ["package.json", ".git"]
    assert len(payload["suggested_rules"]) == 3
    assert payload["suggested_rules"][0]["tool"] == "Bash"
    assert payload["suggested_rules"][0]["pattern"] == "cmd-0"
    assert "detected_at" in payload


def test_write_suggested_rules_skips_when_no_rules(tmp_path: Path) -> None:
    """Avoid leaving an empty file behind that makes ``/allow batch
    approve`` look broken."""
    target = post_clone.write_suggested_rules(
        tmp_path,
        DetectionResult(artifacts_found=[], suggested_rules=[]),
    )
    assert target is None
    assert not (tmp_path / ".whatsbot" / "suggested-rules.json").exists()
