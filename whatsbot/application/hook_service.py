"""HookService — orchestrates the Pre-Tool-Hook decision flow.

C3.2 scope: the real policy is now live — ``evaluate_bash`` from the
domain runs against the project's mode + allow-list, deny-patterns
always fire even in YOLO, and the AskUser branch is delegated to a
``ConfirmationCoordinator`` that performs the async round-trip with
the user's WhatsApp.

Write/Edit classification is still the C3.1 allow-by-default stub —
``path_rules`` land in a subsequent checkpoint.

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

from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.domain.hook_decisions import (
    HookDecision,
    Verdict,
    allow,
    evaluate_bash,
)
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
    ) -> None:
        self._projects = project_repo
        self._rules = allow_rule_repo
        self._coordinator = coordinator
        self._recipient = recipient
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

    def classify_write(
        self,
        *,
        path: str,
        project: str | None,
        session_id: str | None,
    ) -> HookDecision:
        """Decide what to do with a Write/Edit invocation.

        C3.2 keeps the allow-by-default stub — ``path_rules`` lands
        in a later checkpoint. Native write-protection for ``.git``,
        ``.claude`` etc. is enforced inside Claude Code already, so
        this stub doesn't create a dangerous gap in practice.
        """
        self._log.info(
            "hook_write_classified",
            path=path,
            project=project,
            session_id=session_id,
            verdict="allow",
            mode="stub",
        )
        return allow("hook_service stub: path-rules not yet wired")

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
