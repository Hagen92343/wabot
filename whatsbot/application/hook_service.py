"""HookService — orchestrates the Pre-Tool-Hook decision flow.

C3.2 + C4.9 scope: both Bash and Write/Edit now run the full
Spec §12 policy.

* Bash uses ``evaluate_bash`` against the project's mode + allow-
  list; deny-patterns always fire even in YOLO; the AskUser branch
  round-trips through a ``ConfirmationCoordinator``.
* Write/Edit uses ``evaluate_write`` + protected-path check; the
  AskUser branch uses the same coordinator via ``ask_write``.

Dependency wiring intentionally has two shapes:

* **Fully wired** (production): ``project_repo``, ``allow_rule_repo``
  and ``coordinator`` are all supplied. Bash is fully policed; AskUser
  opens a confirmation and awaits the user.
* **Stub mode** (unit tests that only exercise the hook HTTP
  contract): the coordinator is ``None``. We fall back to the
  conservative decisions that mirror the C3.1 stub — allow every call
  so the round-trip can be verified without provisioning state.

Keeping both wirings viable is pragmatic: existing integration tests
for the hook endpoint stay simple, and Phase 4's Claude-launch work
can bolt on the production wiring when the project + allow-rule
sources are already in the app.
"""

from __future__ import annotations

from pathlib import Path

from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.domain.hook_decisions import (
    HookDecision,
    Verdict,
    allow,
    evaluate_bash,
)
from whatsbot.domain.path_rules import evaluate_write
from whatsbot.domain.projects import Mode
from whatsbot.logging_setup import get_logger
from whatsbot.ports.allow_rule_repository import AllowRuleRepository
from whatsbot.ports.project_repository import (
    ProjectNotFoundError,
    ProjectRepository,
)


class HookService:
    """Classifies Pre-Tool-Hook invocations."""

    def __init__(
        self,
        *,
        project_repo: ProjectRepository | None = None,
        allow_rule_repo: AllowRuleRepository | None = None,
        coordinator: ConfirmationCoordinator | None = None,
        recipient: str | None = None,
        projects_root: Path | None = None,
    ) -> None:
        self._projects = project_repo
        self._rules = allow_rule_repo
        self._coordinator = coordinator
        self._recipient = recipient
        self._projects_root = projects_root
        self._log = get_logger("whatsbot.hook")

    # ---- Bash --------------------------------------------------------

    async def classify_bash(
        self,
        *,
        command: str,
        project: str | None,
        session_id: str | None,
    ) -> HookDecision:
        """Decide what to do with a Bash invocation.

        Flow:
          1. If the coordinator is not wired, fall back to allow — the
             endpoint is in stub mode and policing is deferred.
          2. Otherwise run ``evaluate_bash`` against ``(mode, allow-list)``.
          3. If the verdict is AskUser, delegate the async round-trip
             to the coordinator.
        """
        if self._coordinator is None:
            self._log.info(
                "hook_bash_classified",
                command_preview=_preview(command),
                project=project,
                session_id=session_id,
                verdict="allow",
                mode="stub",
            )
            return allow("hook_service stub: coordinator not wired")

        mode, allow_patterns = self._project_context(project)
        decision = evaluate_bash(command, mode=mode, allow_patterns=allow_patterns)

        self._log.info(
            "hook_bash_classified",
            command_preview=_preview(command),
            project=project,
            session_id=session_id,
            verdict=decision.verdict.value,
            mode=mode.value,
            allow_patterns=len(allow_patterns),
        )

        if decision.verdict is not Verdict.ASK_USER:
            return decision

        return await self._coordinator.ask_bash(
            command=command,
            project=project,
            reason=decision.reason,
            msg_id=session_id,
            recipient=self._recipient,
        )

    # ---- Write / Edit -----------------------------------------------

    async def classify_write(
        self,
        *,
        path: str,
        project: str | None,
        session_id: str | None,
    ) -> HookDecision:
        """Decide what to do with a Write/Edit invocation.

        Spec §12 Layer 3 lives in ``domain.path_rules.evaluate_write``;
        this method resolves the project's cwd and the active mode,
        then funnels the AskUser branch through the coordinator.

        Stub fallback (no coordinator wired): return allow so the
        endpoint round-trip remains testable without provisioning.
        """
        if self._coordinator is None:
            self._log.info(
                "hook_write_classified",
                path=path,
                project=project,
                session_id=session_id,
                verdict="allow",
                mode="stub",
            )
            return allow("hook_service stub: coordinator not wired")

        mode, _ = self._project_context(project)
        project_cwd = self._resolve_project_cwd(project)
        decision = evaluate_write(
            Path(path), project_cwd=project_cwd, mode=mode
        )

        self._log.info(
            "hook_write_classified",
            path=path,
            project=project,
            session_id=session_id,
            verdict=decision.verdict.value,
            mode=mode.value,
            has_project_cwd=project_cwd is not None,
        )

        if decision.verdict is not Verdict.ASK_USER:
            return decision

        return await self._coordinator.ask_write(
            path=path,
            project=project,
            reason=decision.reason,
            msg_id=session_id,
            recipient=self._recipient,
        )

    def _resolve_project_cwd(self, project: str | None) -> Path | None:
        """Compute ``<projects_root>/<project>`` or ``None`` if we
        don't have enough context. Pure path math — the directory
        doesn't have to exist on disk for the is-relative-to check."""
        if project is None or self._projects_root is None:
            return None
        return self._projects_root / project

    # ---- helpers ----------------------------------------------------

    def _project_context(self, project: str | None) -> tuple[Mode, list[str]]:
        """Return ``(mode, bash_allow_patterns)`` for ``project``.

        Fail-closed: unknown projects default to Normal + empty
        allow-list, which routes non-deny-matching commands to
        AskUser — safer than assuming YOLO.
        """
        if project is None or self._projects is None or self._rules is None:
            return Mode.NORMAL, []
        try:
            project_row = self._projects.get(project)
        except ProjectNotFoundError:
            self._log.warning("hook_project_unknown", project=project)
            return Mode.NORMAL, []
        rules = self._rules.list_for_project(project)
        patterns = [r.pattern.pattern for r in rules if r.pattern.tool == "Bash"]
        return project_row.mode, patterns


def _preview(value: str, *, max_len: int = 200) -> str:
    """Return a shortened representation for logs. Never logs more than
    ``max_len`` chars so a huge command line can't blow the log budget."""
    if len(value) <= max_len:
        return value
    return value[:max_len] + f"…[+{len(value) - max_len} chars]"
