"""Smart-Detection — read project artefacts, suggest Allow-Rules.

C2.3 covers all nine artefact types from Spec §6 / phase-2.md. Detection
is conservative: we only emit rules for tools that are obviously useful
for the detected stack, never wildcards like ``Bash(*)``.

The function is pure: it inspects a directory tree and returns a
dataclass — it does NOT write the suggested-rules.json file. That side
effect belongs to ``application/post_clone.py``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class AllowRule:
    """One Allow-Rule suggestion. Maps to a row in spec-§19 ``allow_rules``
    when persisted later."""

    tool: str
    pattern: str
    reason: str


@dataclass(frozen=True, slots=True)
class DetectionResult:
    """All artefacts found and the union of Rules they suggest."""

    artifacts_found: list[str]
    suggested_rules: list[AllowRule]


# Artefact -> rule patterns. Each tuple is (tool, pattern). Reasons are
# templated as ``"<artefact> detected"`` so the user can grep
# suggested-rules.json by source.
_ARTEFACT_RULES: dict[str, tuple[tuple[str, str], ...]] = {
    "package.json": (
        ("Bash", "npm test"),
        ("Bash", "npm run *"),
        ("Bash", "npm install"),
        ("Bash", "npm ci"),
        ("Bash", "npx *"),
    ),
    "yarn.lock": (
        ("Bash", "yarn *"),
        ("Bash", "yarn install"),
        ("Bash", "yarn test"),
    ),
    "pnpm-lock.yaml": (
        ("Bash", "pnpm *"),
        ("Bash", "pnpm install"),
    ),
    "pyproject.toml": (
        ("Bash", "uv *"),
        ("Bash", "pytest"),
        ("Bash", "python -m *"),
        ("Bash", "ruff *"),
        ("Bash", "mypy *"),
    ),
    "requirements.txt": (
        ("Bash", "pip install -r requirements.txt"),
        ("Bash", "python -m *"),
        ("Bash", "pytest"),
    ),
    "Cargo.toml": (
        ("Bash", "cargo build"),
        ("Bash", "cargo test"),
        ("Bash", "cargo check"),
        ("Bash", "cargo clippy"),
        ("Bash", "cargo fmt"),
    ),
    "go.mod": (
        ("Bash", "go build"),
        ("Bash", "go test ./*"),
        ("Bash", "go run *"),
        ("Bash", "go mod tidy"),
    ),
    "Makefile": (("Bash", "make *"),),
    "docker-compose": (  # logical key, file detection handled separately
        ("Bash", "docker compose ps"),
        ("Bash", "docker compose logs *"),
        ("Bash", "docker compose up -d"),
        ("Bash", "docker compose down"),
    ),
}

# Artefacts that are detected as files at the project root.
_FILE_ARTEFACTS: tuple[str, ...] = (
    "package.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "pyproject.toml",
    "requirements.txt",
    "Cargo.toml",
    "go.mod",
    "Makefile",
)

# docker-compose ships under either .yml or .yaml. We report both as the
# logical "docker-compose" artefact so the suggested rules don't double up.
_DOCKER_COMPOSE_NAMES: tuple[str, ...] = (
    "docker-compose.yml",
    "docker-compose.yaml",
)

# .git is detected as a directory, not a file.
_GIT_RULES = (
    ("Bash", "git status"),
    ("Bash", "git diff *"),
    ("Bash", "git log *"),
    ("Bash", "git branch *"),
    ("Bash", "git show *"),
    ("Bash", "git remote -v"),
    ("Bash", "git fetch *"),
)


def _rules_for(artefact: str, patterns: tuple[tuple[str, str], ...]) -> list[AllowRule]:
    return [
        AllowRule(tool=tool, pattern=pat, reason=f"{artefact} detected") for (tool, pat) in patterns
    ]


def detect(project_dir: Path) -> DetectionResult:
    """Inspect the project root and return artefact + rule suggestions.

    Defensive: if ``project_dir`` doesn't exist or isn't a directory, the
    result is simply empty — the caller decides whether that's a problem.
    """
    artifacts: list[str] = []
    rules: list[AllowRule] = []

    if not project_dir.is_dir():
        return DetectionResult(artifacts_found=artifacts, suggested_rules=rules)

    # File artefacts in a stable order (matches _FILE_ARTEFACTS).
    for name in _FILE_ARTEFACTS:
        if (project_dir / name).is_file():
            artifacts.append(name)
            rules.extend(_rules_for(name, _ARTEFACT_RULES[name]))

    # docker-compose can be either .yml or .yaml — list whichever is present.
    for compose_name in _DOCKER_COMPOSE_NAMES:
        if (project_dir / compose_name).is_file():
            artifacts.append(compose_name)
            # Use the logical key for the rule set so duplicates collapse if
            # the user accidentally has both files (rare but observed).
            rules.extend(_rules_for(compose_name, _ARTEFACT_RULES["docker-compose"]))

    # .git/ as the final entry to keep the listing readable in WhatsApp.
    if (project_dir / ".git").is_dir():
        artifacts.append(".git")
        rules.extend(AllowRule(tool=t, pattern=p, reason=".git detected") for (t, p) in _GIT_RULES)

    return DetectionResult(artifacts_found=artifacts, suggested_rules=rules)
