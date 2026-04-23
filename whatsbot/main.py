"""whatsbot FastAPI application factory.

Uvicorn-Aufruf via Factory:
    uvicorn whatsbot.main:create_app --factory ...

So bekommen Tests eine eigene App via ``create_app(Settings(env=TEST), ...)``
ohne dass beim Import ein Side-Effect-Setup läuft.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Final

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

import whatsbot
from whatsbot.adapters.keychain_provider import KeychainProvider
from whatsbot.adapters.redacting_sender import RedactingMessageSender
from whatsbot.adapters.sqlite_allow_rule_repository import (
    SqliteAllowRuleRepository,
)
from whatsbot.adapters.sqlite_app_state_repository import (
    SqliteAppStateRepository,
)
from whatsbot.adapters.sqlite_claude_session_repository import (
    SqliteClaudeSessionRepository,
)
from whatsbot.adapters.sqlite_mode_event_repository import (
    SqliteModeEventRepository,
)
from whatsbot.adapters.sqlite_pending_confirmation_repository import (
    SqlitePendingConfirmationRepository,
)
from whatsbot.adapters.sqlite_pending_delete_repository import (
    SqlitePendingDeleteRepository,
)
from whatsbot.adapters.sqlite_pending_output_repository import (
    SqlitePendingOutputRepository,
)
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_repo import open_state_db
from whatsbot.adapters.sqlite_session_lock_repository import (
    SqliteSessionLockRepository,
)
from whatsbot.adapters.subprocess_git_clone import SubprocessGitClone
from whatsbot.adapters.tmux_subprocess import SubprocessTmuxController
from whatsbot.adapters.watchdog_transcript_watcher import (
    WatchdogTranscriptWatcher,
)
from whatsbot.adapters.whatsapp_sender import LoggingMessageSender
from whatsbot.application.active_project_service import ActiveProjectService
from whatsbot.application.allow_service import AllowService
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.confirmation_coordinator import ConfirmationCoordinator
from whatsbot.application.delete_service import DeleteService
from whatsbot.application.force_service import ForceService
from whatsbot.application.hook_service import HookService
from whatsbot.application.kill_service import KillService
from whatsbot.application.lock_service import LockService
from whatsbot.application.mode_service import ModeService
from whatsbot.application.output_service import OutputService
from whatsbot.application.project_service import ProjectService
from whatsbot.application.session_service import SessionService
from whatsbot.application.startup_recovery import StartupRecovery
from whatsbot.application.transcript_ingest import TranscriptIngest
from whatsbot.config import Environment, Settings, assert_secrets_present
from whatsbot.domain import whitelist
from whatsbot.http.hook_endpoint import build_router as build_hook_router
from whatsbot.http.meta_webhook import build_router as build_webhook_router
from whatsbot.http.middleware import ConstantTimeMiddleware, CorrelationIdMiddleware
from whatsbot.logging_setup import configure_logging, get_logger
from whatsbot.ports.git_clone import GitClone
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.secrets_provider import (
    KEY_ALLOWED_SENDERS,
    SecretNotFoundError,
    SecretsProvider,
)
from whatsbot.ports.tmux_controller import TmuxController
from whatsbot.ports.transcript_watcher import TranscriptWatcher

_started_at_monotonic: Final[float] = time.monotonic()


def create_app(
    settings: Settings | None = None,
    secrets_provider: SecretsProvider | None = None,
    message_sender: MessageSender | None = None,
    db_connection: sqlite3.Connection | None = None,
    git_clone: GitClone | None = None,
    projects_root: Path | None = None,
    tmux_controller: TmuxController | None = None,
    safe_claude_binary: str | None = None,
    transcript_watcher: TranscriptWatcher | None = None,
    claude_home: Path | None = None,
    discovery_timeout_seconds: float | None = None,
    run_startup_recovery: bool = False,
) -> FastAPI:
    """Build a fresh FastAPI app. Single entry point for prod, dev and tests."""
    settings = settings if settings is not None else Settings.from_env()

    configure_logging(
        log_dir=settings.log_dir,
        write_to_files=settings.env is not Environment.TEST,
    )
    log = get_logger("whatsbot.startup")

    # Secrets gate (skipped entirely in test env so unit tests don't need a
    # mocked Keychain unless they explicitly inject one).
    if settings.env is not Environment.TEST:
        secrets_provider = secrets_provider if secrets_provider is not None else KeychainProvider()
        missing = assert_secrets_present(secrets_provider, settings)
        if missing:
            log.warning(
                "secrets_missing_dev_mode",
                env=settings.env.value,
                missing=missing,
            )

    # In test env we still need *some* secrets provider to build the webhook
    # router — fall back to an empty stub.
    secrets_for_router: SecretsProvider
    if secrets_provider is not None:
        secrets_for_router = secrets_provider
    else:
        secrets_for_router = _EmptySecretsProvider()

    raw_sender: MessageSender = (
        message_sender if message_sender is not None else LoggingMessageSender()
    )
    # Every outgoing WhatsApp body routes through the Spec §10 redaction
    # pipeline. The decorator is infallible: if redaction would crash
    # (it can't today, it's pure over strings), we still want delivery.
    sender: MessageSender = RedactingMessageSender(raw_sender)

    # State DB: open once per process. In test env caller injects an
    # in-memory connection; otherwise we use the real spec-§4 path.
    conn = db_connection if db_connection is not None else _open_state_db_for(settings)

    # Project store on disk: ~/projekte/<name>/. Tests inject a tmp path.
    projects_root = projects_root if projects_root is not None else Path.home() / "projekte"
    projects_root.mkdir(parents=True, exist_ok=True)

    git_clone_adapter: GitClone = git_clone if git_clone is not None else SubprocessGitClone()

    project_repo = SqliteProjectRepository(conn)
    project_service = ProjectService(
        repository=project_repo,
        conn=conn,
        projects_root=projects_root,
        git_clone=git_clone_adapter,
    )
    allow_service = AllowService(
        rule_repo=SqliteAllowRuleRepository(conn),
        project_repo=project_repo,
        projects_root=projects_root,
    )
    app_state_repo = SqliteAppStateRepository(conn)
    active_project = ActiveProjectService(
        app_state=app_state_repo,
        projects=project_repo,
    )
    delete_service = DeleteService(
        pending_repo=SqlitePendingDeleteRepository(conn),
        project_repo=project_repo,
        app_state=app_state_repo,
        secrets=secrets_for_router,
        projects_root=projects_root,
    )

    # Hook confirmation coordinator (Spec §7 PIN round-trip). Bound to
    # the same event loop as the Meta webhook handler and the hook
    # endpoint so Future.set_result crosses safely between them.
    pending_confirmation_repo = SqlitePendingConfirmationRepository(conn)
    default_recipient = _first_allowed_sender(secrets_for_router)
    coordinator = ConfirmationCoordinator(
        repo=pending_confirmation_repo,
        sender=sender,
        default_recipient=default_recipient,
    )

    # Output-size guard (Spec §10) — >10KB bodies get stashed under
    # ``<data-dir>/outputs/<msg_id>.md`` and the user sees a /send |
    # /discard | /save dialog instead.
    outputs_dir = settings.db_path.parent / "outputs"
    output_service = OutputService(
        sender=sender,
        repo=SqlitePendingOutputRepository(conn),
        outputs_dir=outputs_dir,
    )

    # Phase 4 — SessionService wires tmux + claude_sessions to /p.
    # Constructed only when a TmuxController is available (prod) or
    # injected (tests). Tests that don't exercise /p can omit the
    # controller and the SessionService stays None.
    tmux: TmuxController | None
    if tmux_controller is not None:
        tmux = tmux_controller
    elif settings.env is not Environment.TEST:
        tmux = SubprocessTmuxController()
    else:
        tmux = None

    # Phase 4 C4.2d — transcript-to-WhatsApp pipe.
    #
    # The ingest's on_turn_end callback runs on the watchdog observer
    # thread. It dispatches the assistant text through the output
    # pipeline (redaction + >10KB dialog) to the default WhatsApp
    # recipient — Spec §5 whitelist has a single user in MVP, so
    # ``default_recipient`` above is the right fan-out target.
    #
    # If there's no default recipient (no whitelist configured), we
    # silently drop turn-end callbacks: better than raising on the
    # watcher thread and killing the observer.
    session_service: SessionService | None = None
    lock_service: LockService | None = None
    # C5.5 forward-ref so LockService can call back into the
    # not-yet-constructed SessionService for status-bar repaints.
    # Resolved a few lines below right after SessionService is built.
    session_service_status_ref: list[SessionService | None] = [None]

    def _repaint_status(project: str) -> None:
        svc = session_service_status_ref[0]
        if svc is None:
            return
        svc.repaint_status_bar(project)

    if tmux is not None:
        # Phase 5 LockService — one instance shared by SessionService
        # (acquire before send_prompt), TranscriptIngest (note local
        # input when a non-ZWSP user turn arrives), CommandHandler
        # (/release) and the startup sweeper. ``on_owner_change``
        # repaints the tmux status bar whenever the owner badge
        # would change (C5.5).
        lock_service = LockService(
            repo=SqliteSessionLockRepository(conn),
            on_owner_change=_repaint_status,
        )
        watcher = transcript_watcher
        ingest: TranscriptIngest | None = None
        if watcher is None and settings.env is not Environment.TEST:
            watcher = WatchdogTranscriptWatcher()
        if watcher is not None:
            session_repo_for_ingest = SqliteClaudeSessionRepository(conn)

            def _deliver_turn_end(project: str, text: str) -> None:
                del project  # single-user bot: no per-project routing yet
                if not text:
                    return
                if default_recipient is None:
                    log.warning(
                        "turn_end_dropped_no_recipient",
                        text_len=len(text),
                    )
                    return
                output_service.deliver(to=default_recipient, body=text)

            # C4.8 — when the transcript ingest detects 80% context
            # fill, it needs to shell /compact into the tmux pane.
            # SessionService owns the tmux handle, so the callback
            # closes over it here and the ingest stays decoupled
            # from tmux plumbing.
            session_service_ref: list[SessionService | None] = [None]

            def _fire_compact(project: str) -> None:
                svc = session_service_ref[0]
                if svc is None:
                    return
                svc.fire_auto_compact(project)

            locks_ref = lock_service  # close over the outer instance

            def _note_local_input(project: str) -> None:
                if locks_ref is not None:
                    locks_ref.note_local_input(project)

            ingest = TranscriptIngest(
                session_repo=session_repo_for_ingest,
                on_turn_end=_deliver_turn_end,
                on_auto_compact=_fire_compact,
                on_local_input=_note_local_input,
            )

        session_kwargs: dict[str, object] = {
            "project_repo": project_repo,
            "session_repo": SqliteClaudeSessionRepository(conn),
            "tmux": tmux,
            "projects_root": projects_root,
        }
        if safe_claude_binary is not None:
            session_kwargs["safe_claude_binary"] = safe_claude_binary
        if watcher is not None:
            session_kwargs["transcript_watcher"] = watcher
        if ingest is not None:
            session_kwargs["transcript_ingest"] = ingest
        if claude_home is not None:
            session_kwargs["claude_home"] = claude_home
        if discovery_timeout_seconds is not None:
            session_kwargs["discovery_timeout_seconds"] = discovery_timeout_seconds
        if lock_service is not None:
            session_kwargs["lock_service"] = lock_service
        session_service = SessionService(**session_kwargs)  # type: ignore[arg-type]
        # Resolve the forward ref used by the ingest's auto-compact
        # callback (constructed above before session_service existed).
        if ingest is not None:
            session_service_ref[0] = session_service
        # Resolve the C5.5 status-bar-repaint forward ref so
        # LockService can paint owner changes once SessionService
        # exists.
        session_service_status_ref[0] = session_service

    # ModeService is constructed only when we have a SessionService to
    # recycle; /mode without Claude running has no useful semantics.
    mode_service: ModeService | None = None
    if session_service is not None:
        mode_service = ModeService(
            project_repo=project_repo,
            session_repo=SqliteClaudeSessionRepository(conn),
            mode_event_repo=SqliteModeEventRepository(conn),
            session_service=session_service,
        )

    # ForceService backs ``/force <name> <PIN> <prompt>`` (Phase 5 C5.4).
    # Needs both the lock service (to take over) and the secrets
    # provider (PIN check). Only meaningful when both halves of the
    # send path are wired.
    force_service: ForceService | None = None
    if lock_service is not None and session_service is not None:
        force_service = ForceService(
            lock_service=lock_service,
            project_repo=project_repo,
            secrets=secrets_for_router,
        )

    # KillService backs ``/stop`` and ``/kill`` (Phase 6 C6.1). Needs
    # tmux to do anything useful; lock_service is optional but normally
    # present in the same wiring conditions.
    kill_service: KillService | None = None
    if tmux is not None:
        kill_service = KillService(tmux=tmux, lock_service=lock_service)

    command_handler = CommandHandler(
        project_service=project_service,
        allow_service=allow_service,
        active_project=active_project,
        delete_service=delete_service,
        version=whatsbot.__version__,
        started_at_monotonic=_started_at_monotonic,
        env=settings.env.value,
        session_service=session_service,
        mode_service=mode_service,
        lock_service=lock_service,
        force_service=force_service,
        kill_service=kill_service,
    )

    app = FastAPI(
        title="whatsbot",
        version=whatsbot.__version__,
        description="Persoenlicher WhatsApp-Bot zur Fernsteuerung von Claude Code (single user).",
    )
    # Constant-time padding for /webhook only — avoids slowing /health probes.
    # Spec §5: rejected webhook requests must take the same time as accepted
    # ones so an attacker can't enumerate the sender whitelist via timing.
    app.add_middleware(ConstantTimeMiddleware, min_duration_ms=200, paths=("/webhook",))
    app.add_middleware(CorrelationIdMiddleware)

    app.include_router(
        build_webhook_router(
            settings=settings,
            secrets=secrets_for_router,
            sender=sender,
            command_handler=command_handler,
            coordinator=coordinator,
            output_service=output_service,
        )
    )
    # Stash the coordinator on the app state so ``create_hook_app``
    # (same process, different FastAPI instance) can reuse it.
    app.state.coordinator = coordinator
    app.state.project_repo = project_repo
    app.state.allow_rule_repo = SqliteAllowRuleRepository(conn)
    app.state.projects_root = projects_root
    # Exposed for tests that need to drive lifecycle ops from outside
    # the webhook flow (e.g. stop_transcript_watch on teardown).
    app.state.session_service = session_service

    # Phase 4 C4.6 + C4.7 — StartupRecovery coerces YOLO → Normal
    # (Spec §6 invariant) and calls ensure_started for every row in
    # claude_sessions so surviving sessions come back up after a
    # bot restart. Opt-in so pre-existing test paths (which create
    # ephemeral in-memory DBs with no recoverable sessions) don't
    # pay the cost.
    if run_startup_recovery and session_service is not None:
        recovery = StartupRecovery(
            project_repo=project_repo,
            session_repo=SqliteClaudeSessionRepository(conn),
            mode_event_repo=SqliteModeEventRepository(conn),
            session_service=session_service,
        )
        recovery.run()
        app.state.startup_recovery = recovery

    @app.get("/health", tags=["meta"])
    async def health() -> dict[str, object]:
        return {
            "ok": True,
            "version": whatsbot.__version__,
            "uptime_seconds": round(time.monotonic() - _started_at_monotonic, 3),
            "env": settings.env.value,
        }

    @app.get("/metrics", tags=["meta"], response_class=PlainTextResponse)
    async def metrics() -> str:
        # Phase 1 stub. Real Prometheus exposition lands in Phase 8.
        return ""

    log.info(
        "startup_complete",
        env=settings.env.value,
        dry_run=settings.dry_run,
        version=whatsbot.__version__,
    )
    return app


def create_hook_app(
    settings: Settings | None = None,
    secrets_provider: SecretsProvider | None = None,
    hook_service: HookService | None = None,
    main_app: FastAPI | None = None,
) -> FastAPI:
    """Build the Pre-Tool-Hook app (Spec §7, §14).

    Separate FastAPI instance because Spec §14 requires it to bind only
    to ``127.0.0.1:8001`` (the Meta webhook app is on ``:8000``). Both
    apps share the same process in production — deploy-launchd will
    invoke this factory with ``uvicorn whatsbot.main:create_hook_app
    --factory --host 127.0.0.1 --port 8001`` alongside the main listener.

    If a ``main_app`` is passed in, its coordinator + project-repo +
    allow-rule-repo are reused so the hook round-trip shares the same
    in-memory confirmation registry as the Meta webhook handler. This
    is the Phase-4 wiring path; stand-alone hook-app tests keep using
    the stub ``HookService`` with no deps.
    """
    settings = settings if settings is not None else Settings.from_env()
    configure_logging(
        log_dir=settings.log_dir,
        write_to_files=settings.env is not Environment.TEST,
    )
    log = get_logger("whatsbot.startup")

    if secrets_provider is None:
        secrets_provider = (
            KeychainProvider() if settings.env is not Environment.TEST else _EmptySecretsProvider()
        )
    if hook_service is None:
        if main_app is not None:
            hook_service = HookService(
                project_repo=main_app.state.project_repo,
                allow_rule_repo=main_app.state.allow_rule_repo,
                coordinator=main_app.state.coordinator,
                projects_root=main_app.state.projects_root,
            )
        else:
            hook_service = HookService()

    app = FastAPI(
        title="whatsbot-hook",
        version=whatsbot.__version__,
        description="Pre-Tool-Hook endpoint (Spec §7). Bind ONLY to 127.0.0.1.",
    )
    app.add_middleware(CorrelationIdMiddleware)
    app.include_router(build_hook_router(secrets=secrets_provider, service=hook_service))

    @app.get("/health", tags=["meta"])
    async def hook_health() -> dict[str, object]:
        return {
            "ok": True,
            "component": "hook",
            "version": whatsbot.__version__,
        }

    log.info(
        "hook_startup_complete",
        env=settings.env.value,
        version=whatsbot.__version__,
    )
    return app


def _open_state_db_for(settings: Settings) -> sqlite3.Connection:
    """Open the spec-§4 state DB. Centralised so tests can monkeypatch
    if they need to redirect production-path access."""
    return open_state_db(db_path=settings.db_path, backup_dir=settings.backup_dir)


def _first_allowed_sender(secrets_provider: SecretsProvider) -> str | None:
    """Return the first whitelisted WhatsApp number (used as the default
    recipient for unsolicited bot messages like hook-confirmation
    prompts). ``None`` if the whitelist is empty/missing."""
    try:
        raw = secrets_provider.get(KEY_ALLOWED_SENDERS)
    except SecretNotFoundError:
        return None
    allowed = whitelist.parse_whitelist(raw)
    if not allowed:
        return None
    return sorted(allowed)[0]


class _EmptySecretsProvider:
    """Fallback for the test env when no provider is injected.

    Returns ``SecretNotFoundError`` for every key, which the webhook router
    treats as "no whitelist, no app secret, no verify token" — exactly what
    we want for unit tests that exercise routing logic without supplying
    Keychain content.
    """

    def get(self, key: str) -> str:
        from whatsbot.ports.secrets_provider import SecretNotFoundError

        raise SecretNotFoundError(f"empty provider has no {key!r}")

    def set(self, key: str, value: str) -> None:  # pragma: no cover
        raise NotImplementedError

    def rotate(self, key: str, new_value: str) -> None:  # pragma: no cover
        raise NotImplementedError
