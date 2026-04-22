"""Unit tests for whatsbot.domain.smart_detection (all 9 artefacts)."""

from __future__ import annotations

from pathlib import Path

import pytest

from whatsbot.domain.smart_detection import detect

pytestmark = pytest.mark.unit


# --- empty / missing -------------------------------------------------------


def test_empty_dir_returns_no_artifacts(tmp_path: Path) -> None:
    result = detect(tmp_path)
    assert result.artifacts_found == []
    assert result.suggested_rules == []


def test_missing_dir_returns_empty(tmp_path: Path) -> None:
    """Defensive: a non-existent path must not crash."""
    result = detect(tmp_path / "ghost")
    assert result.artifacts_found == []
    assert result.suggested_rules == []


# --- per-artefact happy paths ----------------------------------------------


def test_package_json_yields_npm_rules(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    result = detect(tmp_path)
    assert "package.json" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {"npm test", "npm run *", "npm install", "npm ci", "npx *"} <= patterns
    for rule in result.suggested_rules:
        assert rule.reason == "package.json detected"


def test_yarn_lock_yields_yarn_rules(tmp_path: Path) -> None:
    (tmp_path / "yarn.lock").write_text("", encoding="utf-8")
    result = detect(tmp_path)
    assert "yarn.lock" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {"yarn *", "yarn install", "yarn test"} <= patterns


def test_pnpm_lock_yields_pnpm_rules(tmp_path: Path) -> None:
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    result = detect(tmp_path)
    assert "pnpm-lock.yaml" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {"pnpm *", "pnpm install"} <= patterns


def test_pyproject_yields_python_tooling_rules(tmp_path: Path) -> None:
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    result = detect(tmp_path)
    assert "pyproject.toml" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {"uv *", "pytest", "python -m *", "ruff *", "mypy *"} <= patterns


def test_requirements_txt_yields_pip_rules(tmp_path: Path) -> None:
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    result = detect(tmp_path)
    assert "requirements.txt" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert "pip install -r requirements.txt" in patterns
    assert "pytest" in patterns
    assert "python -m *" in patterns


def test_cargo_toml_yields_cargo_rules(tmp_path: Path) -> None:
    (tmp_path / "Cargo.toml").write_text("[package]\nname='x'\n", encoding="utf-8")
    result = detect(tmp_path)
    assert "Cargo.toml" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {
        "cargo build",
        "cargo test",
        "cargo check",
        "cargo clippy",
        "cargo fmt",
    } <= patterns


def test_go_mod_yields_go_rules(tmp_path: Path) -> None:
    (tmp_path / "go.mod").write_text("module example.com/x\n", encoding="utf-8")
    result = detect(tmp_path)
    assert "go.mod" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {"go build", "go test ./*", "go run *", "go mod tidy"} <= patterns


def test_makefile_yields_make_rule(tmp_path: Path) -> None:
    (tmp_path / "Makefile").write_text("all:\n\techo ok\n", encoding="utf-8")
    result = detect(tmp_path)
    assert "Makefile" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert "make *" in patterns


@pytest.mark.parametrize("name", ["docker-compose.yml", "docker-compose.yaml"])
def test_docker_compose_yields_compose_rules(tmp_path: Path, name: str) -> None:
    (tmp_path / name).write_text("services: {}\n", encoding="utf-8")
    result = detect(tmp_path)
    assert name in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {
        "docker compose ps",
        "docker compose logs *",
        "docker compose up -d",
        "docker compose down",
    } <= patterns


def test_git_dir_yields_git_rules(tmp_path: Path) -> None:
    (tmp_path / ".git").mkdir()
    result = detect(tmp_path)
    assert ".git" in result.artifacts_found
    patterns = {r.pattern for r in result.suggested_rules}
    assert {
        "git status",
        "git diff *",
        "git log *",
        "git branch *",
        "git show *",
        "git remote -v",
        "git fetch *",
    } <= patterns


# --- combos ----------------------------------------------------------------


def test_combined_npm_and_git_yield_union(tmp_path: Path) -> None:
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    result = detect(tmp_path)
    assert set(result.artifacts_found) == {"package.json", ".git"}
    # 5 npm rules + 7 git rules = 12 total
    assert len(result.suggested_rules) == 12


def test_python_plus_make_plus_compose_plus_git(tmp_path: Path) -> None:
    """Realistic Python-stack project: pyproject + Makefile + docker-compose
    + git all at the root."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "Makefile").write_text("all:\n", encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n", encoding="utf-8")
    (tmp_path / ".git").mkdir()

    result = detect(tmp_path)
    found = set(result.artifacts_found)
    assert found == {"pyproject.toml", "Makefile", "docker-compose.yml", ".git"}
    # 5 + 1 + 4 + 7 = 17
    assert len(result.suggested_rules) == 17


def test_artefact_listing_order_is_stable(tmp_path: Path) -> None:
    """File artefacts come first (in declaration order), then docker-compose
    (if present), then .git/. WhatsApp output relies on this for readability."""
    (tmp_path / ".git").mkdir()
    (tmp_path / "Makefile").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    result = detect(tmp_path)
    # package.json is declared before Makefile in _FILE_ARTEFACTS, .git last.
    assert result.artifacts_found.index("package.json") < result.artifacts_found.index("Makefile")
    assert result.artifacts_found.index(".git") == len(result.artifacts_found) - 1


def test_all_suggested_rules_target_bash_tool(tmp_path: Path) -> None:
    """Spec §6 / phase-2.md: all 9 stacks emit Bash rules. Read/Edit
    suggestions are intentionally NOT auto-generated — those need user
    judgement."""
    for f in (
        "package.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements.txt",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "docker-compose.yml",
    ):
        (tmp_path / f).write_text("", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    result = detect(tmp_path)
    assert {r.tool for r in result.suggested_rules} == {"Bash"}


# --- defensive cases -------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        "package.json",
        "yarn.lock",
        "pnpm-lock.yaml",
        "pyproject.toml",
        "requirements.txt",
        "Cargo.toml",
        "go.mod",
        "Makefile",
        "docker-compose.yml",
    ],
)
def test_artefact_must_be_a_file_not_a_directory(tmp_path: Path, name: str) -> None:
    """A hostile repo could have a *directory* named ``package.json``; we
    must not treat any such directory as the corresponding artefact."""
    (tmp_path / name).mkdir()
    result = detect(tmp_path)
    assert name not in result.artifacts_found


def test_dot_git_must_be_a_directory_not_a_file(tmp_path: Path) -> None:
    """Symmetric guard for the .git case (file submodule pointers exist
    in real repos but they are not full git dirs)."""
    (tmp_path / ".git").write_text("gitdir: ../foo/.git/modules/x", encoding="utf-8")
    result = detect(tmp_path)
    assert ".git" not in result.artifacts_found
