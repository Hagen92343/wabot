"""CommandHandler — dispatch inbound text to the right Use-Case.

Phase 1 had a pure ``domain.commands.route`` function because the only
commands (`/ping`, `/status`, `/help`) needed no I/O. Phase 2 introduces
project-management commands that mutate state, so the dispatcher moves
into the application layer where service injection is natural.

The pure command bodies still live in ``domain.commands`` — this handler
just routes the new ones and delegates the old ones unchanged.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import replace
from typing import Final

from whatsbot.application.project_service import (
    ProjectFilesystemError,
    ProjectService,
)
from whatsbot.domain import commands
from whatsbot.domain.commands import CommandResult, StatusSnapshot
from whatsbot.domain.git_url import DisallowedGitUrlError
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    format_listing,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.project_repository import ProjectAlreadyExistsError

_NEW_PREFIX: Final = "/new "
_LS_COMMAND: Final = "/ls"


class CommandHandler:
    """Stateful handler that owns references to all the services the
    commands need. One instance per process; safe to call from multiple
    request handlers since the underlying SQLite connection is thread-safe
    enough for the single-user bot.
    """

    def __init__(
        self,
        *,
        project_service: ProjectService,
        version: str,
        started_at_monotonic: float,
        env: str,
        db_ok_callback: Callable[[], bool] | None = None,
    ) -> None:
        self._projects = project_service
        self._version = version
        self._started_at = started_at_monotonic
        self._env = env
        self._db_ok_callback = db_ok_callback
        self._log = get_logger("whatsbot.commands")

    # ---- entrypoint -------------------------------------------------------

    def handle(self, text: str) -> CommandResult:
        cmd = text.strip()

        # Project-management commands first — they have arguments.
        if cmd.startswith(_NEW_PREFIX):
            return self._handle_new(cmd[len(_NEW_PREFIX) :].strip())
        if cmd == _LS_COMMAND:
            return self._handle_list()

        # Phase-1 commands fall through to the pure router.
        return commands.route(cmd, self._snapshot())

    # ---- /new <name> ------------------------------------------------------

    def _handle_new(self, args: str) -> CommandResult:
        parts = args.split()

        # /new <name> git <url>
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
            f"   /allow batch approve  — alle übernehmen (kommt in C2.4)\n"
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
        listings = self._projects.list_all()
        return CommandResult(reply=format_listing(listings), command="/ls")

    # ---- helpers ----------------------------------------------------------

    def _snapshot(self) -> StatusSnapshot:
        return StatusSnapshot(
            version=self._version,
            uptime_seconds=time.monotonic() - self._started_at,
            db_ok=self._db_ok_callback() if self._db_ok_callback else True,
            env=self._env,
        )

    # Allow tests to swap the snapshot fields without touching the clock.
    def _snapshot_with(self, **overrides: object) -> StatusSnapshot:
        return replace(self._snapshot(), **overrides)  # type: ignore[arg-type]
