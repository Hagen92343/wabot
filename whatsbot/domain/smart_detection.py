"""Smart-Detection — read project artefacts, suggest Allow-Rules.

C2.2 covers two artefact types (``package.json`` and ``.git``) so the
end-to-end flow (clone → detect → suggest → store) can be exercised.
C2.3 expands to all nine artefact types from Spec §6 / phase-2.md
(yarn/pnpm, pyproject, requirements, Cargo, go.mod, Makefile,
docker-compose).

The function is pure: it inspects a directory tree and returns a
dataclass — it does not write the suggested-rules.json file. That side
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


# C2.2 detection table — additions land in C2.3.
_PACKAGE_JSON_RULES = (
    ("Bash", "npm test"),
    ("Bash", "npm run *"),
    ("Bash", "npm install"),
    ("Bash", "npm ci"),
    ("Bash", "npx *"),
)

_GIT_RULES = (
    ("Bash", "git status"),
    ("Bash", "git diff *"),
    ("Bash", "git log *"),
    ("Bash", "git branch *"),
    ("Bash", "git show *"),
    ("Bash", "git remote -v"),
    ("Bash", "git fetch *"),
)


def detect(project_dir: Path) -> DetectionResult:
    """Inspect the project root and return artefact + rule suggestions.

    Defensive: if ``project_dir`` doesn't exist or isn't a directory, the
    result is simply empty — the caller decides whether that's a problem.
    """
    artifacts: list[str] = []
    rules: list[AllowRule] = []

    if not project_dir.is_dir():
        return DetectionResult(artifacts_found=artifacts, suggested_rules=rules)

    if (project_dir / "package.json").is_file():
        artifacts.append("package.json")
        rules.extend(
            AllowRule(tool=t, pattern=p, reason="package.json detected")
            for (t, p) in _PACKAGE_JSON_RULES
        )

    if (project_dir / ".git").is_dir():
        artifacts.append(".git")
        rules.extend(AllowRule(tool=t, pattern=p, reason=".git detected") for (t, p) in _GIT_RULES)

    return DetectionResult(artifacts_found=artifacts, suggested_rules=rules)
