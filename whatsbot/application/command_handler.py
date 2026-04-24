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
from whatsbot.application.delete_service import (
    DeleteService,
    InvalidPinError,
    NoPendingDeleteError,
    PanicPinNotConfiguredError,
    PendingDeleteExpiredError,
)
from whatsbot.application.diagnostics_service import DiagnosticsService
from whatsbot.application.force_service import ForceService
from whatsbot.application.kill_service import KillService
from whatsbot.application.limit_service import MaxLimitActiveError
from whatsbot.application.lock_service import (
    LocalTerminalHoldsLockError,
    LockService,
)
from whatsbot.application.lockdown_service import LockdownService
from whatsbot.application.mode_service import (
    InvalidModeTransitionError,
    ModeService,
)
from whatsbot.application.panic_service import PanicService
from whatsbot.application.project_service import (
    ProjectFilesystemError,
    ProjectService,
)
from whatsbot.application.session_service import SessionService
from whatsbot.application.unlock_service import UnlockService
from whatsbot.domain import commands
from whatsbot.domain.allow_rules import (
    InvalidAllowRuleError,
    format_pattern,
)
from whatsbot.domain.commands import CommandResult, StatusSnapshot
from whatsbot.domain.git_url import DisallowedGitUrlError
from whatsbot.domain.limits import format_reset_duration
from whatsbot.domain.pending_deletes import CONFIRM_WINDOW_SECONDS
from whatsbot.domain.projects import (
    InvalidProjectNameError,
    Mode,
    format_listing,
    validate_project_name,
)
from whatsbot.logging_setup import get_logger
from whatsbot.ports.project_repository import (
    ProjectAlreadyExistsError,
    ProjectNotFoundError,
)
from whatsbot.ports.tmux_controller import TmuxError

_NEW_PREFIX: Final = "/new "
_LS_COMMAND: Final = "/ls"
_P_COMMAND: Final = "/p"
_P_PREFIX: Final = "/p "
_ALLOWLIST_COMMAND: Final = "/allowlist"
_ALLOW_PREFIX: Final = "/allow "
_DENY_PREFIX: Final = "/deny "
_RM_PREFIX: Final = "/rm "
_MODE_COMMAND: Final = "/mode"
_MODE_PREFIX: Final = "/mode "
_RELEASE_COMMAND: Final = "/release"
_RELEASE_PREFIX: Final = "/release "
_FORCE_PREFIX: Final = "/force "
_STOP_COMMAND: Final = "/stop"
_STOP_PREFIX: Final = "/stop "
_KILL_COMMAND: Final = "/kill"
_KILL_PREFIX: Final = "/kill "
_PANIC_COMMAND: Final = "/panic"
_UNLOCK_PREFIX: Final = "/unlock "
_UNLOCK_COMMAND: Final = "/unlock"
# Phase 8 C8.2 — diagnostics commands (Spec §11).
_LOG_COMMAND: Final = "/log"
_LOG_PREFIX: Final = "/log "
_ERRORS_COMMAND: Final = "/errors"
_PS_COMMAND: Final = "/ps"
_UPDATE_COMMAND: Final = "/update"

# Spec §7 lockdown filter — these are the only commands the bot
# answers while lockdown is engaged. /unlock is the way out;
# /help, /ping, /status are read-only diagnostics that are
# safe to answer (and useful — the user wants to know it's
# really their bot before tipping the PIN).
_LOCKDOWN_ALLOWED_PREFIXES: Final = (_UNLOCK_PREFIX, _LOG_PREFIX)
# /errors + /ps + /log are read-only diagnostics safe under lockdown
# — they help the user figure out *why* lockdown engaged before they
# commit the PIN.
_LOCKDOWN_ALLOWED_COMMANDS: Final = frozenset(
    {
        _UNLOCK_COMMAND,
        _ERRORS_COMMAND,
        _PS_COMMAND,
        _LOG_COMMAND,
        _UPDATE_COMMAND,
        "/help",
        "/ping",
        "/status",
    }
)


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
        delete_service: DeleteService,
        version: str,
        started_at_monotonic: float,
        env: str,
        db_ok_callback: Callable[[], bool] | None = None,
        session_service: SessionService | None = None,
        mode_service: ModeService | None = None,
        lock_service: LockService | None = None,
        force_service: ForceService | None = None,
        kill_service: KillService | None = None,
        panic_service: PanicService | None = None,
        unlock_service: UnlockService | None = None,
        lockdown_service: LockdownService | None = None,
        diagnostics_service: DiagnosticsService | None = None,
    ) -> None:
        self._projects = project_service
        self._allow = allow_service
        self._active = active_project
        self._delete = delete_service
        self._version = version
        self._started_at = started_at_monotonic
        self._env = env
        self._db_ok_callback = db_ok_callback
        self._sessions = session_service
        self._modes = mode_service
        self._locks = lock_service
        self._force = force_service
        self._kill = kill_service
        self._panic = panic_service
        self._unlock = unlock_service
        self._lockdown = lockdown_service
        self._diagnostics = diagnostics_service
        self._log = get_logger("whatsbot.commands")

    # ---- entrypoint -------------------------------------------------------

    def handle(self, text: str) -> CommandResult:
        cmd = text.strip()

        # Phase 6 C6.6 — Lockdown filter. When the bot is in
        # lockdown (panic + watchdog can both engage it), every
        # command except a tiny allow-list is dropped with a hint.
        # /unlock is the only escape; /help/ping/status are
        # diagnostics. The filter runs *before* dispatch so a
        # downstream service can't accidentally do anything.
        lockdown_block = self._maybe_block_for_lockdown(cmd)
        if lockdown_block is not None:
            return lockdown_block

        # Project-management
        if cmd.startswith(_NEW_PREFIX):
            return self._handle_new(cmd[len(_NEW_PREFIX) :].strip())
        if cmd == _LS_COMMAND:
            return self._handle_list()

        # Active project
        if cmd == _P_COMMAND:
            return self._handle_show_active()
        if cmd.startswith(_P_PREFIX):
            return self._handle_p_args(cmd[len(_P_PREFIX) :].strip())

        # Allow rules
        if cmd == _ALLOWLIST_COMMAND:
            return self._handle_allowlist()
        if cmd.startswith(_ALLOW_PREFIX):
            return self._handle_allow(cmd[len(_ALLOW_PREFIX) :].strip())
        if cmd.startswith(_DENY_PREFIX):
            return self._handle_deny(cmd[len(_DENY_PREFIX) :].strip())

        # /rm <name> [<PIN>]
        if cmd.startswith(_RM_PREFIX):
            return self._handle_rm(cmd[len(_RM_PREFIX) :].strip())

        # /mode + /mode <normal|strict|yolo>
        if cmd == _MODE_COMMAND:
            return self._handle_show_mode()
        if cmd.startswith(_MODE_PREFIX):
            return self._handle_set_mode(cmd[len(_MODE_PREFIX) :].strip())

        # /release + /release <name> (Phase 5)
        if cmd == _RELEASE_COMMAND:
            return self._handle_release(None)
        if cmd.startswith(_RELEASE_PREFIX):
            return self._handle_release(
                cmd[len(_RELEASE_PREFIX) :].strip()
            )

        # /force <name> <PIN> <prompt> (Phase 5 C5.4)
        if cmd.startswith(_FORCE_PREFIX):
            return self._handle_force(cmd[len(_FORCE_PREFIX) :].strip())

        # /stop + /stop <name> (Phase 6 C6.1)
        if cmd == _STOP_COMMAND:
            return self._handle_stop(None)
        if cmd.startswith(_STOP_PREFIX):
            return self._handle_stop(cmd[len(_STOP_PREFIX) :].strip())

        # /kill + /kill <name> (Phase 6 C6.1)
        if cmd == _KILL_COMMAND:
            return self._handle_kill(None)
        if cmd.startswith(_KILL_PREFIX):
            return self._handle_kill(cmd[len(_KILL_PREFIX) :].strip())

        # /panic — no PIN by Spec §5 (low friction in an emergency).
        if cmd == _PANIC_COMMAND:
            return self._handle_panic()

        # /unlock <PIN> — PIN-gated lockdown release (Phase 6 C6.6).
        if cmd.startswith(_UNLOCK_PREFIX):
            return self._handle_unlock(cmd[len(_UNLOCK_PREFIX) :].strip())
        if cmd == _UNLOCK_COMMAND:
            return CommandResult(
                reply="Verwendung: /unlock <PIN>",
                command="/unlock",
            )

        # Phase 8 C8.2 — diagnostics commands.
        if cmd.startswith(_LOG_PREFIX):
            return self._handle_log(cmd[len(_LOG_PREFIX) :].strip())
        if cmd == _LOG_COMMAND:
            return self._handle_log("")
        if cmd == _ERRORS_COMMAND:
            return self._handle_errors()
        if cmd == _PS_COMMAND:
            return self._handle_ps()
        if cmd == _UPDATE_COMMAND:
            return self._handle_update()

        # Non-slash text → prompt to the active project (Phase-4
        # C4.2c). Empty text falls through to the default help
        # response so /webhook never ships a zero-length reply.
        if cmd and not cmd.startswith("/"):
            routed = self._handle_bare_prompt(cmd)
            if routed is not None:
                return routed

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

    def _handle_p_args(self, args: str) -> CommandResult:
        """Route ``/p <name> [<prompt>...]`` — one word switches active,
        multi-word forwards the prompt to <name>'s session without
        changing the active pointer (Spec §11 ``/p <n> <prompt>``).
        """
        parts = args.split(maxsplit=1)
        if len(parts) == 1:
            return self._handle_set_active(parts[0])
        raw_name, prompt = parts[0], parts[1].strip()
        if not prompt:
            return self._handle_set_active(raw_name)
        return self._handle_project_prompt(raw_name, prompt)

    def _handle_set_active(self, raw_name: str) -> CommandResult:
        try:
            name = self._active.set_active(raw_name)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/p")
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc} Tippe /ls fuer Liste.", command="/p")

        # C4.1d: switching active project ensures the tmux session + Claude
        # process are running. If session_service isn't wired (older tests,
        # pre-Phase-4 paths) we just skip the launch and hand back the same
        # reply as before. Failures are logged and surface as a warning
        # suffix — the active pointer stays set so the user can /force or
        # retry later.
        suffix = ""
        if self._sessions is not None:
            try:
                self._sessions.ensure_started(name)
            except (TmuxError, FileNotFoundError, OSError) as exc:
                self._log.error(
                    "ensure_started_failed",
                    project=name,
                    error=str(exc),
                )
                suffix = "\n⚠️ Claude-Session konnte nicht gestartet werden."
        return CommandResult(
            reply=f"▶ aktiv: {name}{suffix}",
            command="/p",
        )

    def _handle_project_prompt(
        self, raw_name: str, prompt: str
    ) -> CommandResult:
        """``/p <name> <prompt>`` — send a one-shot prompt to ``<name>``
        without switching the active project pointer. Spec §11."""
        try:
            name = validate_project_name(raw_name)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/p")
        return self._dispatch_prompt(name, prompt, command="/p")

    def _handle_bare_prompt(self, text: str) -> CommandResult | None:
        """Non-slash text → forward to active project's Claude session.
        Returns ``None`` when there's no session_service wired, so the
        caller falls through to the default help hint (keeps older
        test paths working)."""
        if self._sessions is None:
            return None
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply=(
                    "kein aktives Projekt. Setze eines mit /p <name> oder "
                    "prompte direkt: /p <name> <prompt>."
                ),
                command="<prompt>",
            )
        return self._dispatch_prompt(active, text, command="<prompt>")

    def _dispatch_prompt(
        self, name: str, prompt: str, *, command: str
    ) -> CommandResult:
        if self._sessions is None:
            # Session service not wired — treat the prompt as a no-op
            # with a visible hint rather than silently swallowing it.
            return CommandResult(
                reply="⚠️ Claude-Session nicht konfiguriert.",
                command=command,
            )
        try:
            self._sessions.send_prompt(name, prompt)
        except ProjectNotFoundError as exc:
            return CommandResult(
                reply=f"⚠️ {exc} Tippe /ls fuer Liste.", command=command
            )
        except LocalTerminalHoldsLockError:
            # Spec §7 soft-preemption: local terminal holds the lock.
            # Tell the user how to override.
            return CommandResult(
                reply=(
                    f"🔒 Terminal aktiv auf '{name}'. "
                    f"Benutze /force {name} <PIN> <prompt> zum "
                    "Uebernehmen oder /release zum Freigeben."
                ),
                command=command,
            )
        except MaxLimitActiveError as exc:
            # Spec §14 — no queueing, hard-reject with the shortest
            # active limit's countdown so the user knows when to
            # retry.
            import time

            now = int(time.time())
            reset_str = format_reset_duration(
                exc.limit.reset_at_ts, now=now
            )
            return CommandResult(
                reply=(
                    f"⏸ Max-Limit erreicht [{exc.limit.kind.value}] · "
                    f"Reset in {reset_str}"
                ),
                command=command,
            )
        except (TmuxError, FileNotFoundError, OSError) as exc:
            self._log.error(
                "send_prompt_failed",
                project=name,
                error=str(exc),
            )
            return CommandResult(
                reply=f"⚠️ Prompt an '{name}' nicht zustellbar.",
                command=command,
            )
        return CommandResult(
            reply=f"📨 an {name}: {_preview(prompt)}",
            command=command,
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

    # ---- /rm <name> [<PIN>] ----------------------------------------------

    def _handle_rm(self, args: str) -> CommandResult:
        parts = args.split()
        if len(parts) not in (1, 2):
            return CommandResult(
                reply=(
                    "Verwendung:\n"
                    "  /rm <name>         — initiiere Löschung (60s Fenster)\n"
                    "  /rm <name> <PIN>   — bestätigen"
                ),
                command="/rm",
            )

        if len(parts) == 1:
            return self._handle_rm_request(parts[0])
        return self._handle_rm_confirm(parts[0], parts[1])

    def _handle_rm_request(self, raw_name: str) -> CommandResult:
        try:
            pending = self._delete.request_delete(raw_name)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/rm")
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/rm")
        return CommandResult(
            reply=(
                f"🗑 Bestätige mit /rm {pending.project_name} <PIN> "
                f"innerhalb {CONFIRM_WINDOW_SECONDS}s."
            ),
            command="/rm",
        )

    def _handle_rm_confirm(self, raw_name: str, pin: str) -> CommandResult:
        try:
            outcome = self._delete.confirm_delete(raw_name, pin)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/rm")
        except NoPendingDeleteError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/rm")
        except PendingDeleteExpiredError as exc:
            return CommandResult(reply=f"⌛ {exc}", command="/rm")
        except InvalidPinError:
            return CommandResult(reply="⚠️ Falsche PIN.", command="/rm")
        except PanicPinNotConfiguredError as exc:
            self._log.error("panic_pin_missing", error=str(exc))
            return CommandResult(reply=f"⚠️ {exc}", command="/rm")
        if outcome.trashed_to is None:
            # Imported project: we didn't create the dir, so we don't delete it.
            reply = (
                f"🗑 '{outcome.project_name}' entregistriert "
                f"(Ordner unberührt)."
            )
        else:
            reply = (
                f"🗑 '{outcome.project_name}' gelöscht "
                f"(verschoben nach {outcome.trashed_to})."
            )
        return CommandResult(reply=reply, command="/rm")

    # ---- /mode [normal|strict|yolo] --------------------------------------

    _MODE_EMOJI: Final = {
        Mode.NORMAL: "🟢",
        Mode.STRICT: "🔵",
        Mode.YOLO: "🔴",
    }

    def _handle_show_mode(self) -> CommandResult:
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/mode",
            )
        if self._modes is None:
            return CommandResult(
                reply="⚠️ Mode-Service nicht konfiguriert.",
                command="/mode",
            )
        try:
            mode = self._modes.show_mode(active)
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/mode")
        emoji = self._MODE_EMOJI.get(mode, "·")
        return CommandResult(
            reply=f"{emoji} {mode.value} ({active})",
            command="/mode",
        )

    def _handle_set_mode(self, raw: str) -> CommandResult:
        target = raw.strip().lower()
        valid = {m.value for m in Mode}
        if target not in valid:
            return CommandResult(
                reply=(
                    "Verwendung: /mode (normal | strict | yolo)"
                ),
                command="/mode",
            )
        active = self._active.get_active()
        if active is None:
            return CommandResult(
                reply="kein aktives Projekt — setze eines mit /p <name>.",
                command="/mode",
            )
        if self._modes is None:
            return CommandResult(
                reply="⚠️ Mode-Service nicht konfiguriert.",
                command="/mode",
            )
        target_mode = Mode(target)
        try:
            outcome = self._modes.change_mode(active, target_mode)
        except ProjectNotFoundError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/mode")
        except InvalidModeTransitionError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/mode")
        except (TmuxError, FileNotFoundError, OSError) as exc:
            self._log.error(
                "mode_switch_failed",
                project=active,
                target=target_mode.value,
                error=str(exc),
            )
            return CommandResult(
                reply=f"⚠️ Mode-Wechsel gescheitert: {exc}",
                command="/mode",
            )

        emoji = self._MODE_EMOJI.get(outcome.to_mode, "·")
        if outcome.was_noop:
            reply = f"{emoji} bereits {outcome.to_mode.value} ({active})"
        else:
            reply = (
                f"{emoji} {outcome.from_mode.value} → {outcome.to_mode.value} "
                f"({active})"
            )
        return CommandResult(reply=reply, command="/mode")

    # ---- /release [name] (Phase 5) ---------------------------------------

    def _handle_release(self, raw_name: str | None) -> CommandResult:
        """Drop the session lock back to ``free`` for the named (or
        active) project. Idempotent — nothing-to-release still gets
        a friendly confirmation."""
        if self._locks is None:
            return CommandResult(
                reply="⚠️ Lock-Service nicht konfiguriert.",
                command="/release",
            )

        if raw_name:
            try:
                name = validate_project_name(raw_name)
            except InvalidProjectNameError as exc:
                return CommandResult(reply=f"⚠️ {exc}", command="/release")
        else:
            active = self._active.get_active()
            if active is None:
                return CommandResult(
                    reply=(
                        "kein aktives Projekt — setze eines mit /p <name> "
                        "oder gib /release <name> an."
                    ),
                    command="/release",
                )
            name = active

        existed = self._locks.release(name)
        if existed:
            return CommandResult(
                reply=f"🔓 Lock fuer '{name}' freigegeben.",
                command="/release",
            )
        return CommandResult(
            reply=f"'{name}' hatte keinen aktiven Lock.",
            command="/release",
        )

    # ---- /force <name> <PIN> <prompt> (Phase 5 C5.4) ---------------------

    def _handle_force(self, args: str) -> CommandResult:
        """PIN-gated lock override + prompt delivery (Spec §11).

        Sequence on success:

        1. ``ForceService.force(name, pin)`` validates the project,
           verifies the PIN against the Keychain ``panic-pin``, and
           takes the bot lock unconditionally.
        2. ``SessionService.send_prompt(name, prompt)`` ships the
           prompt to tmux. Because the bot now owns the lock, the
           inner ``acquire_for_bot`` round inside ``send_prompt``
           also succeeds.

        On PIN failure we never touch the lock — the local terminal
        keeps it.
        """
        if self._force is None or self._sessions is None:
            return CommandResult(
                reply="⚠️ Force-Service nicht konfiguriert.",
                command="/force",
            )

        parts = args.split(maxsplit=2)
        if len(parts) != 3:
            return CommandResult(
                reply=(
                    "Verwendung:\n"
                    "  /force <name> <PIN> <prompt>"
                ),
                command="/force",
            )

        raw_name, pin, prompt = parts[0], parts[1], parts[2].strip()
        if not prompt:
            return CommandResult(
                reply="Verwendung: /force <name> <PIN> <prompt>",
                command="/force",
            )

        try:
            outcome = self._force.force(raw_name, pin)
        except InvalidProjectNameError as exc:
            return CommandResult(reply=f"⚠️ {exc}", command="/force")
        except ProjectNotFoundError as exc:
            return CommandResult(
                reply=f"⚠️ {exc} Tippe /ls fuer Liste.", command="/force"
            )
        except InvalidPinError:
            return CommandResult(reply="⚠️ Falsche PIN.", command="/force")
        except PanicPinNotConfiguredError as exc:
            self._log.error("panic_pin_missing", error=str(exc))
            return CommandResult(reply=f"⚠️ {exc}", command="/force")

        # Lock is now ours. Hand the prompt off — send_prompt's inner
        # acquire_for_bot will see the BOT lock and pass through.
        try:
            self._sessions.send_prompt(outcome.project_name, prompt)
        except (TmuxError, FileNotFoundError, OSError) as exc:
            self._log.error(
                "force_send_prompt_failed",
                project=outcome.project_name,
                error=str(exc),
            )
            return CommandResult(
                reply=(
                    f"🔓 Lock fuer '{outcome.project_name}' uebernommen, "
                    "aber Prompt nicht zustellbar."
                ),
                command="/force",
            )

        return CommandResult(
            reply=(
                f"🔓 Lock fuer '{outcome.project_name}' uebernommen.\n"
                f"📨 an {outcome.project_name}: {_preview(prompt)}"
            ),
            command="/force",
        )

    # ---- /stop + /kill (Phase 6 C6.1) ------------------------------------

    def _resolve_target_project(
        self, raw_name: str | None, *, command: str
    ) -> tuple[str | None, CommandResult | None]:
        """Pick the project ``raw_name`` refers to, defaulting to the
        active one. Returns ``(name, None)`` on success or
        ``(None, error_reply)`` when the user needs a hint.
        """
        if raw_name:
            try:
                return validate_project_name(raw_name), None
            except InvalidProjectNameError as exc:
                return None, CommandResult(reply=f"⚠️ {exc}", command=command)
        active = self._active.get_active()
        if active is None:
            return None, CommandResult(
                reply=(
                    "kein aktives Projekt — setze eines mit /p <name> "
                    f"oder gib {command} <name> an."
                ),
                command=command,
            )
        return active, None

    def _handle_stop(self, raw_name: str | None) -> CommandResult:
        """Send Ctrl+C to the named (or active) project's tmux pane."""
        if self._kill is None:
            return CommandResult(
                reply="⚠️ Kill-Service nicht konfiguriert.",
                command="/stop",
            )
        name, err = self._resolve_target_project(raw_name, command="/stop")
        if err is not None:
            return err
        assert name is not None
        try:
            outcome = self._kill.stop(name)
        except (TmuxError, FileNotFoundError, OSError) as exc:
            self._log.error("stop_failed", project=name, error=str(exc))
            return CommandResult(
                reply=f"⚠️ /stop an '{name}' fehlgeschlagen: {exc}",
                command="/stop",
            )
        if not outcome.was_alive:
            return CommandResult(
                reply=f"'{outcome.project_name}' hatte keine aktive Session.",
                command="/stop",
            )
        return CommandResult(
            reply=f"🛑 Ctrl+C an '{outcome.project_name}' geschickt.",
            command="/stop",
        )

    def _handle_kill(self, raw_name: str | None) -> CommandResult:
        """Destroy the project's tmux session. Lock gets released."""
        if self._kill is None:
            return CommandResult(
                reply="⚠️ Kill-Service nicht konfiguriert.",
                command="/kill",
            )
        name, err = self._resolve_target_project(raw_name, command="/kill")
        if err is not None:
            return err
        assert name is not None
        try:
            outcome = self._kill.kill(name)
        except (TmuxError, FileNotFoundError, OSError) as exc:
            self._log.error("kill_failed", project=name, error=str(exc))
            return CommandResult(
                reply=f"⚠️ /kill an '{name}' fehlgeschlagen: {exc}",
                command="/kill",
            )
        if not outcome.was_alive:
            return CommandResult(
                reply=f"'{outcome.project_name}' hatte keine aktive Session.",
                command="/kill",
            )
        suffix = " · Lock freigegeben" if outcome.lock_released else ""
        return CommandResult(
            reply=(
                f"🪓 '{outcome.project_name}' tmux-Session beendet{suffix}."
            ),
            command="/kill",
        )

    # ---- /panic (Phase 6 C6.2) ------------------------------------------

    def _handle_panic(self) -> CommandResult:
        """Run the full Spec §7 panic playbook.

        Bewusst kein PIN (Spec §5): in Panik soll der User nicht erst
        ein Passwort tippen müssen. Lockdown nach Panic blockiert
        ohnehin alle weiteren Befehle bis ``/unlock <PIN>``.
        """
        if self._panic is None:
            return CommandResult(
                reply="⚠️ Panic-Service nicht konfiguriert.",
                command="/panic",
            )
        try:
            outcome = self._panic.panic()
        except Exception as exc:
            self._log.exception(
                "panic_failed", error=str(exc)
            )
            return CommandResult(
                reply=(
                    "⚠️ Panic ist mitten im Lauf gescheitert. "
                    "Pruefe /errors am Mac."
                ),
                command="/panic",
            )
        ms = round(outcome.duration_seconds * 1000)
        bits: list[str] = [
            f"🚨 PANIC! {len(outcome.sessions_killed)} Sessions getötet"
        ]
        if outcome.yolo_projects_reset:
            bits.append(
                f"{len(outcome.yolo_projects_reset)} YOLO → Normal"
            )
        if outcome.locks_released:
            bits.append(f"{len(outcome.locks_released)} Locks freigegeben")
        bits.append(f"in {ms} ms")
        return CommandResult(
            reply=(
                ", ".join(bits) + ".\n"
                "Bot ist im Lockdown. /unlock <PIN> zum Aufheben."
            ),
            command="/panic",
        )

    # ---- /unlock <PIN> + Lockdown filter (Phase 6 C6.6) ------------------

    def _handle_unlock(self, pin: str) -> CommandResult:
        """Verify PIN, disengage lockdown."""
        if self._unlock is None:
            return CommandResult(
                reply="⚠️ Unlock-Service nicht konfiguriert.",
                command="/unlock",
            )
        if not pin:
            return CommandResult(
                reply="Verwendung: /unlock <PIN>",
                command="/unlock",
            )
        try:
            outcome = self._unlock.unlock(pin)
        except InvalidPinError:
            return CommandResult(reply="⚠️ Falsche PIN.", command="/unlock")
        except PanicPinNotConfiguredError as exc:
            self._log.error("panic_pin_missing", error=str(exc))
            return CommandResult(reply=f"⚠️ {exc}", command="/unlock")
        if not outcome.was_engaged:
            return CommandResult(
                reply="🔓 Bot war nicht im Lockdown.",
                command="/unlock",
            )
        return CommandResult(
            reply="🔓 Lockdown aufgehoben.",
            command="/unlock",
        )

    # ---- /log /errors /ps /update (Phase 8 C8.2) --------------------------

    def _handle_log(self, args: str) -> CommandResult:
        if self._diagnostics is None:
            return CommandResult(
                reply="⚠️ Diagnostics nicht verfügbar.",
                command="/log",
            )
        msg_id = args.strip()
        if not msg_id:
            return CommandResult(
                reply=(
                    "Verwendung: /log <msg_id>\n"
                    "msg_id aus /errors oder /ps kopieren, "
                    "oder aus einer früheren Bot-Antwort."
                ),
                command="/log",
            )
        entries = self._diagnostics.read_trace(msg_id)
        reply = self._diagnostics.format_trace(msg_id, entries)
        return CommandResult(reply=reply, command="/log")

    def _handle_errors(self) -> CommandResult:
        if self._diagnostics is None:
            return CommandResult(
                reply="⚠️ Diagnostics nicht verfügbar.",
                command="/errors",
            )
        entries = self._diagnostics.recent_errors()
        reply = self._diagnostics.format_errors(entries)
        return CommandResult(reply=reply, command="/errors")

    def _handle_ps(self) -> CommandResult:
        if self._diagnostics is None:
            return CommandResult(
                reply="⚠️ Diagnostics nicht verfügbar.",
                command="/ps",
            )
        snaps = self._diagnostics.active_sessions()
        reply = self._diagnostics.format_sessions(snaps)
        return CommandResult(reply=reply, command="/ps")

    def _handle_update(self) -> CommandResult:
        if self._diagnostics is None:
            # Fallback-Hint ohne DiagnosticsService — das ist reine
            # Text-Ausgabe, kein echter State-Zugriff nötig.
            return CommandResult(
                reply=(
                    "Claude-Code-Updates laufen manuell. "
                    "Details: docs/RUNBOOK.md §Update."
                ),
                command="/update",
            )
        return CommandResult(
            reply=self._diagnostics.format_update_hint(),
            command="/update",
        )

    def _maybe_block_for_lockdown(self, cmd: str) -> CommandResult | None:
        """Spec §7 lockdown filter.

        Called from the top of ``handle()``. Returns a friendly
        block-message ``CommandResult`` if lockdown is engaged and
        ``cmd`` is *not* in the allow-list; ``None`` otherwise (in
        which case the dispatcher proceeds normally).
        """
        if self._lockdown is None:
            return None
        if not self._lockdown.is_engaged():
            return None
        # Allow-list: bare commands or any command in the prefix list.
        if cmd in _LOCKDOWN_ALLOWED_COMMANDS:
            return None
        for prefix in _LOCKDOWN_ALLOWED_PREFIXES:
            if cmd.startswith(prefix):
                return None
        # Anything else: surface lockdown + how to clear it.
        return CommandResult(
            reply=(
                "🔒 Bot ist im Lockdown. /unlock <PIN> zum Aufheben."
            ),
            command="<lockdown>",
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


# ---- prompt-ack preview helper ------------------------------------------


_PROMPT_PREVIEW_CHARS: Final = 60


def _preview(text: str) -> str:
    """Single-line, ≤60 char preview of a prompt for the /p ack.

    Prompts can be long or multi-line — the ack is a confirmation,
    not a mirror of the full text. Newlines collapse to spaces so
    the WhatsApp render stays tight.
    """
    collapsed = " ".join(text.split())
    if len(collapsed) <= _PROMPT_PREVIEW_CHARS:
        return collapsed
    return collapsed[: _PROMPT_PREVIEW_CHARS - 1] + "…"
