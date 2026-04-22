"""CommandHandler — dispatch inbound text to the right Use-Case.

Phase 1 had a pure ``domain.commands.route`` function because the only
commands needed no I/O. Phase 2 introduces project-management commands
that mutate state, so the dispatcher moves into the application layer
where service injection is natural.

The pure command bodies still live in ``domain.commands`` — this handler
just routes the new ones and delegates the old ones unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Final

from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import (
    AllowService,
    NoSuggestedRulesError,
)
from whatsbot.application.project_service import (
    ProjectFilesystemError,
    ProjectService,
)
from whatsbot.domain import commands
from whatsbot.domain.allow_rules import (
    InvalidAllowRuleError,
    format_pattern,
)
from whatsbot.domain.commands import CommandResult, StatusSnapshot
from whatsbot.domain.git_url import DisallowedGitUrlError
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    format_listing,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.project_repository import (
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
)

_NEW_PREFIX: Final = "/new "
_LS_COMMAND: Final = "/ls"
_P_COMMAND: Final = "/p"
_P_PREFIX: Final = "/p "
_ALLOWLIST_COMMAND: Final = "/allowlist"
_ALLOW_PREFIX: Final = "/allow "
_DENY_PREFIX: Final = "/deny "


class CommandHandler:
    """Stateful handler that owns references to all the services the
    commands need. One instance per process; safe to call from multiple
    request handlers since the underlying SQLite connection is thread-safe
    enough for the single-user bot."""

    def __init__(
        self,
        *,
        project_service: ProjectService,
        allow_service: AllowService,
        active_project: ActiveProjectService,
        version: str,
        started_at_monotonic: float,
        env: str,
        db_ok_callback: Callable[[], bool] | None = None,
    ) -> None:
        self._projects = project_service
        self._allow = allow_service
        self._active = active_project
        self._version = version
        self._started_at = started_at_monotonic
        self._env = env
        self._db_ok_callback = db_ok_callback
        self._log = get_logger("whatsbot.commands")

    # ---- entrypoint -------------------------------------------------------

    def handle(self, text: str) -> CommandResult:
        cmd = text.strip()

        # Project-management
        if cmd.startswith(_NEW_PREFIX):
            return self._handle_new(cmd[len(_NEW_PREFIX) :].strip())
        if cmd == _LS_COMMAND:
            return self._handle_list()

        # Active project
        if cmd == _P_COMMAND:
            return self._handle_show_active()
        if cmd.startswith(_P_PREFIX):
            return self._handle_set_active(cmd[len(_P_PREFIX) :].strip())

        # Allow rules
        if cmd == _ALLOWLIST_COMMAND:
            return self._handle_allowlist()
        if cmd.startswith(_ALLOW_PREFIX):
            return self._handle_allow(cmd[len(_ALLOW_PREFIX) :].strip())
        if cmd.startswith(_DENY_PREFIX):
            return self._handle_deny(cmd[len(_DENY_PREFIX) :].strip())

        # Phase-1 commands fall through to the pure router.
        return commands.route(cmd, self._snapshot())

    # ---- /new <name> ------------------------------------------------------

    def _handle_new(self, args: str) -> CommandResult:
        parts = args.split()

        if len(parts) == 3 and parts[1] == "git":
            return self._handle_new_git(name=parts[0], url=parts[2])

        if len(parts) != 1:
            return CommandResult(
                reply=(
                    "Verwendung:\n"
                    "  /new <name>            — leeres Projekt\n"
                    "  /new <name> git <url>  — Git-Klon\n"
                    "Name: 2-32 Zeichen, klein, '_' oder '-' erlaubt."
                ),
                command="/new",
            )
        raw_name = parts[0]
        try:
            project = self._projects.create_empty(raw_name)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/new")
        except ProjectAlreadyExistsError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/new")
        except ProjectFilesystemError as exc:
            self._log.error("project_create_fs_failed", name=raw_name, error=str(exc))
            return CommandResult(reply=f"⚠️ {exc}", command="/new")
        return CommandResult(
            reply=(
                f"✅ Projekt '{project.name}' angelegt "
                f"({project.source_mode.value} · {project.mode.value})"
            ),
            command="/new",
        )

    def _handle_new_git(self, *, name: str, url: str) -> CommandResult:
        try:
            outcome = self._projects.create_from_git(name, url)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/new git")
        except DisallowedGitUrlError as exc:
            return CommandResult(reply=f"🚫 {exc}", command="/new git")
        except ProjectAlreadyExistsError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/new git")
        except ProjectFilesystemError as exc:
            self._log.error(
                "project_create_git_failed",
                name=name,
                url=url,
                error=str(exc),
            )
            return CommandResult(reply=f"⚠️ {exc}", command="/new git")

        suggestions = len(outcome.detection.suggested_rules)
        artefacts = ", ".join(outcome.detection.artifacts_found) or "keine bekannten"
        suffix = (
            f"\n💡 {suggestions} Rule-Vorschläge aus {artefacts}.\n"
            f"   /allow batch approve  — alle übernehmen\n"
            f"   /allow batch review   — einzeln anschauen"
            if suggestions
            else f"\n(keine Allow-Rule-Vorschläge — Artefakte: {artefacts})"
        )
        return CommandResult(
            reply=(
                f"✅ Projekt '{outcome.project.name}' geklont "
                f"({outcome.project.mode.value})"
                f"{suffix}"
            ),
            command="/new git",
        )

    # ---- /ls --------------------------------------------------------------

    def _handle_list(self) -> CommandResult:
        active = self._active.get_active()
        listings = self._projects.list_all(active_name=active)
        return CommandResult(reply=format_listing(listings), command="/ls")

    # ---- /p (active project) ---------------------------------------------

    def _handle_show_active(self) -> CommandResult:
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt. Setze eines mit /p <name>.",
                command="/p",
            )
        return CommandResult(
            reply=f"aktives Projekt: ▶ {active}",
            command="/p",
        )

    def _handle_set_active(self, raw_name: str) -> CommandResult:
        try:
            name = self._active.set_active(raw_name)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/p")
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc} Tippe /ls fuer Liste.", command="/p")
        return CommandResult(
            reply=f"▶ aktiv: {name}",
            command="/p",
        )

    # ---- /allow + /deny + /allowlist + /allow batch * --------------------

    def _handle_allow(self, args: str) -> CommandResult:
        # /allow batch approve | review
        if args.startswith("batch "):
            sub = args[len("batch ") :].strip()
            if sub == "approve":
                return self._handle_allow_batch_approve()
            if sub == "review":
                return self._handle_allow_batch_review()
            return CommandResult(
                reply="Verwendung: /allow batch (approve | review)",
                command="/allow batch",
            )

        # Single manual rule
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/allow",
            )
        try:
            stored = self._allow.add_manual(active, args)
        except InvalidAllowRuleError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/allow")
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/allow")
        return CommandResult(
            reply=f"✅ Rule hinzugefügt: {format_pattern(stored.pattern)}",
            command="/allow",
        )

    def _handle_deny(self, raw_pattern: str) -> CommandResult:
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/deny",
            )
        try:
            removed = self._allow.remove(active, raw_pattern)
        except InvalidAllowRuleError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/deny")
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/deny")
        if not removed:
            return CommandResult(
                reply=f"⚠️ Rule '{raw_pattern}' war nicht in der Allow-Liste.",
                command="/deny",
            )
        return CommandResult(
            reply=f"🗑 Rule entfernt: {raw_pattern}",
            command="/deny",
        )

    def _handle_allowlist(self) -> CommandResult:
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/allowlist",
            )
        rules = self._allow.list_rules(active)
        if not rules:
            return CommandResult(
                reply=f"({active}) noch keine Allow-Rules.",
                command="/allowlist",
            )
        # Group by source for readability.
        grouped: dict[str, list[str]] = {}
        for rule in rules:
            grouped.setdefault(rule.source.value, []).append(format_pattern(rule.pattern))
        lines = [f"Allow-Rules fuer '{active}':"]
        for source in ("default", "smart_detection", "manual"):
            entries = grouped.get(source, [])
            if not entries:
                continue
            lines.append(f"  [{source}]")
            for entry in entries:
                lines.append(f"    {entry}")
        return CommandResult(reply="\n".join(lines), command="/allowlist")

    def _handle_allow_batch_review(self) -> CommandResult:
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/allow batch review",
            )
        suggestions = self._allow.batch_review(active)
        if not suggestions:
            return CommandResult(
                reply=f"({active}) keine Vorschlaege offen.",
                command="/allow batch review",
            )
        lines = [f"Vorschlaege fuer '{active}' ({len(suggestions)}):"]
        for idx, entry in enumerate(suggestions, start=1):
            lines.append(f"  {idx:>2}. {format_pattern(entry.pattern)}   ({entry.reason})")
        lines.append("")
        lines.append("Tippe /allow batch approve um alle zu uebernehmen.")
        return CommandResult(
            reply="\n".join(lines),
            command="/allow batch review",
        )

    def _handle_allow_batch_approve(self) -> CommandResult:
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/allow batch approve",
            )
        try:
            outcome = self._allow.batch_approve(active)
        except NoSuggestedRulesError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/allow batch approve")
        return CommandResult(
            reply=(
                f"✅ {len(outcome.added)} neue Rules in '{active}' uebernommen "
                f"(bereits vorhanden: {len(outcome.already_present)})."
            ),
            command="/allow batch approve",
        )

    # ---- helpers ----------------------------------------------------------

    def _snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            version=self._version,
            uptime_seconds=time.monotonic() - self._started_at,
            db_ok=self._db_ok_callback() if self._db_ok_callback else True,
            env=self._env,
        )

    def _snapshot_with(self, **overrides: object) -> StatusSnapshot:
        return replace(self._snapshot(), **overrides)  # type: ignore[arg-type]
