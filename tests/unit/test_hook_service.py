"""Unit tests for whatsbot.application.hook_service.

C3.2 scope: classify_bash is async and runs evaluate_bash when the
coordinator is wired; otherwise it falls back to the C3.1 allow-by-
default stub. classify_write is unchanged (still stub) — path_rules
are on the roadmap for a later checkpoint.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.application.hook_service import HookService
from whatsbot.domain.allow_rules import AllowRulePattern, AllowRuleSource
from whatsbot.domain.hook_decisions import HookDecision, Verdict, allow, deny
from whatsbot.domain.projects import Mode, Project, SourceMode
from whatsbot.ports.allow_rule_repository import StoredAllowRule
from whatsbot.ports.project_repository import ProjectNotFoundError

pytestmark = pytest.mark.unit


# --- Stub repos + coordinator --------------------------------------------


class _StubProjectRepo:
    """Implements the ProjectRepository Protocol for hook-service tests.

    Only ``get`` is exercised here; the remaining methods satisfy the
    Protocol so ``mypy --strict`` accepts this stub as a substitute.
    """

    def __init__(self, projects: dict[str, Project]) -> None:
        self._projects = projects

    def get(self, name: str) -> Project:
        try:
            return self._projects[name]
        except KeyError as exc:
            raise ProjectNotFoundError(f"unknown project: {name}") from exc

    def list_all(self) -> list[Project]:
        return list(self._projects.values())

    def create(self, project: Project) -> None:  # pragma: no cover
        self._projects[project.name] = project

    def delete(self, name: str) -> None:  # pragma: no cover
        if name not in self._projects:
            raise ProjectNotFoundError(name)
        del self._projects[name]

    def exists(self, name: str) -> bool:  # pragma: no cover
        return name in self._projects

    def update_mode(self, name: str, mode: Mode) -> None:  # pragma: no cover
        if name not in self._projects:
            raise ProjectNotFoundError(name)
        existing = self._projects[name]
        self._projects[name] = Project(
            name=existing.name,
            source_mode=existing.source_mode,
            source=existing.source,
            created_at=existing.created_at,
            last_used_at=existing.last_used_at,
            default_model=existing.default_model,
            mode=mode,
        )


class _StubAllowRuleRepo:
    """Implements the AllowRuleRepository Protocol for hook-service tests."""

    def __init__(self, rules: dict[str, list[StoredAllowRule]]) -> None:
        self._rules = rules

    def list_for_project(self, project_name: str) -> list[StoredAllowRule]:
        return list(self._rules.get(project_name, ()))

    def add(  # pragma: no cover
        self,
        project_name: str,
        pattern: AllowRulePattern,
        source: AllowRuleSource,
    ) -> StoredAllowRule:
        rule = _rule(project_name, pattern.tool, pattern.pattern, rule_id=999)
        self._rules.setdefault(project_name, []).append(rule)
        return rule

    def remove(  # pragma: no cover
        self, project_name: str, pattern: AllowRulePattern
    ) -> bool:
        existing = self._rules.get(project_name, [])
        kept = [r for r in existing if r.pattern != pattern]
        removed = len(kept) != len(existing)
        self._rules[project_name] = kept
        return removed


class _SpyCoordinator(ConfirmationCoordinator):
    """Records ask_bash calls and returns a predetermined decision.

    Subclasses ConfirmationCoordinator so the HookService constructor
    accepts it under mypy-strict — but all the real machinery is
    overridden, so no actual Futures / DB / sender interaction happens.
    """

    def __init__(self, *, decision: HookDecision) -> None:
        self._decision = decision
        self.calls: list[dict[str, object]] = []

    async def ask_bash(self, **kwargs: object) -> HookDecision:
        self.calls.append(kwargs)
        return self._decision

    async def ask_write(self, **kwargs: object) -> HookDecision:
        self.calls.append(kwargs)
        return self._decision


def _project(name: str, mode: Mode = Mode.NORMAL) -> Project:
    return Project(
        name=name,
        source_mode=SourceMode.EMPTY,
        created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
        mode=mode,
    )


def _rule(project: str, tool: str, pattern: str, rule_id: int = 1) -> StoredAllowRule:
    return StoredAllowRule(
        id=rule_id,
        project_name=project,
        pattern=AllowRulePattern(tool=tool, pattern=pattern),
        source=AllowRuleSource.MANUAL,
        created_at=datetime(2026, 4, 22, 12, 0, tzinfo=UTC),
    )


# --- Stub mode (no coordinator wired) -----------------------------------


def test_stub_mode_allows_everything() -> None:
    svc = HookService()
    d = asyncio.run(svc.classify_bash(command="ls", project="alpha", session_id="s1"))
    assert d.verdict is Verdict.ALLOW
    assert "stub" in d.reason


def test_stub_mode_allows_even_dangerous_commands() -> None:
    # The hook endpoint does defence-in-depth on top of this — stub
    # mode deliberately doesn't police anything so integration tests
    # stay easy to write.
    svc = HookService()
    d = asyncio.run(svc.classify_bash(command="rm -rf /", project=None, session_id=None))
    assert d.verdict is Verdict.ALLOW


def test_classify_write_stub_allows() -> None:
    """Stub mode (no coordinator wired) — still falls back to allow
    for the HTTP-contract tests that don't provision a DB."""
    svc = HookService()
    d = asyncio.run(
        svc.classify_write(
            path="/Users/me/projekte/alpha/README.md",
            project="alpha",
            session_id="s1",
        )
    )
    assert d.verdict is Verdict.ALLOW


# --- Fully wired: deny-patterns fire -----------------------------------


def test_bash_deny_pattern_blocks_in_normal_mode() -> None:
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.NORMAL)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("should never reach"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="rm -rf /", project="alpha", session_id="s1")
    )
    assert d.verdict is Verdict.DENY
    assert "rm -rf /" in d.reason
    assert coordinator.calls == []  # no AskUser when deny fires


def test_bash_deny_pattern_blocks_in_yolo_mode() -> None:
    """Deny is the last line of defence — it must fire even in YOLO."""
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.YOLO)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("should never reach"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="sudo apt install", project="alpha", session_id="s1")
    )
    assert d.verdict is Verdict.DENY
    assert coordinator.calls == []


# --- Fully wired: allow-rules short-circuit AskUser --------------------


def test_bash_allow_rule_short_circuits_ask_in_normal() -> None:
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.NORMAL)})
    rules = _StubAllowRuleRepo({"alpha": [_rule("alpha", "Bash", "npm test")]})
    coordinator = _SpyCoordinator(decision=allow("should never reach"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="npm test", project="alpha", session_id="s1")
    )
    assert d.verdict is Verdict.ALLOW
    assert "Bash(npm test)" in d.reason
    assert coordinator.calls == []


def test_bash_non_bash_allow_rules_are_ignored() -> None:
    # Write-scoped allow rules must not be confused for Bash scope.
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.STRICT)})
    rules = _StubAllowRuleRepo({"alpha": [_rule("alpha", "Write", "ls")]})
    coordinator = _SpyCoordinator(decision=allow("never"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(svc.classify_bash(command="ls", project="alpha", session_id="s1"))
    assert d.verdict is Verdict.DENY  # strict + no matching rule
    assert coordinator.calls == []


# --- Fully wired: mode fall-through ------------------------------------


def test_bash_normal_unknown_command_delegates_to_coordinator() -> None:
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.NORMAL)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("user confirmed"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(
            command="make deploy",
            project="alpha",
            session_id="s42",
        )
    )
    assert d.verdict is Verdict.ALLOW  # the spy returned allow
    assert coordinator.calls and coordinator.calls[0]["command"] == "make deploy"
    assert coordinator.calls[0]["project"] == "alpha"
    assert coordinator.calls[0]["msg_id"] == "s42"


def test_bash_strict_unknown_command_denies_silently() -> None:
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.STRICT)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=deny("should never reach"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="anything", project="alpha", session_id="s1")
    )
    assert d.verdict is Verdict.DENY
    assert coordinator.calls == []


def test_bash_yolo_unknown_command_allows_without_asking() -> None:
    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.YOLO)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=deny("should never reach"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="anything", project="alpha", session_id="s1")
    )
    assert d.verdict is Verdict.ALLOW
    assert coordinator.calls == []


# --- Fully wired: unknown project falls back to Normal + empty allow ---


def test_bash_unknown_project_defaults_to_normal_mode() -> None:
    projects = _StubProjectRepo({})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=deny("timeout"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="echo hi", project="ghost", session_id="s1")
    )
    # Unknown project → NORMAL + no allow-list → AskUser →
    # coordinator returns deny (our spy).
    assert d.verdict is Verdict.DENY
    assert coordinator.calls  # was asked


def test_bash_no_project_defaults_to_normal_and_asks() -> None:
    projects = _StubProjectRepo({})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("ok"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
    )
    d = asyncio.run(
        svc.classify_bash(command="ls", project=None, session_id=None)
    )
    assert d.verdict is Verdict.ALLOW
    assert coordinator.calls


# --- Huge command input -------------------------------------------------


def test_extreme_command_does_not_raise() -> None:
    svc = HookService()
    huge = "echo " + "x" * 5000
    d = asyncio.run(svc.classify_bash(command=huge, project=None, session_id=None))
    assert d.verdict is Verdict.ALLOW  # stub mode


# --- classify_write (C4.9) -------------------------------------------------


def test_classify_write_project_scope_allows_without_asking(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    projects_root = _Path(str(tmp_path)) / "projekte"
    project_dir = projects_root / "alpha"
    project_dir.mkdir(parents=True)

    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.NORMAL)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=deny("should not fire"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
        projects_root=projects_root,
    )
    d = asyncio.run(
        svc.classify_write(
            path=str(project_dir / "src" / "main.py"),
            project="alpha",
            session_id="s1",
        )
    )
    assert d.verdict is Verdict.ALLOW
    # Project-scope paths don't ask the human.
    assert coordinator.calls == []


def test_classify_write_protected_denies_even_in_yolo(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    projects_root = _Path(str(tmp_path)) / "projekte"
    project_dir = projects_root / "alpha"
    project_dir.mkdir(parents=True)

    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.YOLO)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("should not fire"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
        projects_root=projects_root,
    )
    d = asyncio.run(
        svc.classify_write(
            path=str(project_dir / ".git" / "config"),
            project="alpha",
            session_id="s1",
        )
    )
    assert d.verdict is Verdict.DENY
    # Deny-path short-circuits — coordinator not consulted.
    assert coordinator.calls == []


def test_classify_write_outside_project_strict_denies_silently(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    projects_root = _Path(str(tmp_path)) / "projekte"
    (projects_root / "alpha").mkdir(parents=True)

    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.STRICT)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("should not fire"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
        projects_root=projects_root,
    )
    d = asyncio.run(
        svc.classify_write(
            path="/etc/hosts",
            project="alpha",
            session_id="s1",
        )
    )
    assert d.verdict is Verdict.DENY
    # Strict silent-deny — no human round-trip.
    assert coordinator.calls == []


def test_classify_write_outside_project_normal_asks_user(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    projects_root = _Path(str(tmp_path)) / "projekte"
    (projects_root / "alpha").mkdir(parents=True)

    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.NORMAL)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=allow("user approved"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
        projects_root=projects_root,
    )
    d = asyncio.run(
        svc.classify_write(
            path="/etc/hosts",
            project="alpha",
            session_id="s1",
        )
    )
    assert d.verdict is Verdict.ALLOW
    # Coordinator WAS consulted.
    assert len(coordinator.calls) == 1
    assert coordinator.calls[0].get("path") == "/etc/hosts"


def test_classify_write_outside_project_yolo_allows_without_asking(
    tmp_path: object,
) -> None:
    from pathlib import Path as _Path

    projects_root = _Path(str(tmp_path)) / "projekte"
    (projects_root / "alpha").mkdir(parents=True)

    projects = _StubProjectRepo({"alpha": _project("alpha", Mode.YOLO)})
    rules = _StubAllowRuleRepo({})
    coordinator = _SpyCoordinator(decision=deny("should not fire"))
    svc = HookService(
        project_repo=projects,
        allow_rule_repo=rules,
        coordinator=coordinator,
        projects_root=projects_root,
    )
    d = asyncio.run(
        svc.classify_write(
            path="/etc/hosts",
            project="alpha",
            session_id="s1",
        )
    )
    assert d.verdict is Verdict.ALLOW
    assert coordinator.calls == []
