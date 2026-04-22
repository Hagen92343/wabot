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
from whatsbot.adapters.sqlite_project_repository import SqliteProjectRepository
from whatsbot.adapters.sqlite_repo import open_state_db
from whatsbot.adapters.whatsapp_sender import LoggingMessageSender
from whatsbot.application.command_handler import CommandHandler
from whatsbot.application.project_service import ProjectService
from whatsbot.config import Environment, Settings, assert_secrets_present
from whatsbot.http.meta_webhook import build_router as build_webhook_router
from whatsbot.http.middleware import ConstantTimeMiddleware, CorrelationIdMiddleware
from whatsbot.logging_setup import configure_logging, get_logger
from whatsbot.ports.message_sender import MessageSender
from whatsbot.ports.secrets_provider import SecretsProvider

_started_at_monotonic: Final[float] = time.monotonic()


def create_app(
    settings: Settings | None = None,
    secrets_provider: SecretsProvider | None = None,
    message_sender: MessageSender | None = None,
    db_connection: sqlite3.Connection | None = None,
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

    sender: MessageSender = message_sender if message_sender is not None else LoggingMessageSender()

    # State DB: open once per process. In test env caller injects an
    # in-memory connection; otherwise we use the real spec-§4 path.
    conn = db_connection if db_connection is not None else _open_state_db_for(settings)

    # Project store on disk: ~/projekte/<name>/. Configurable via env later
    # if needed, hardcoded for now matching the spec default.
    projects_root = Path.home() / "projekte"
    projects_root.mkdir(parents=True, exist_ok=True)

    project_service = ProjectService(
        repository=SqliteProjectRepository(conn),
        conn=conn,
        projects_root=projects_root,
    )

    command_handler = CommandHandler(
        project_service=project_service,
        version=whatsbot.__version__,
        started_at_monotonic=_started_at_monotonic,
        env=settings.env.value,
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
        )
    )

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


def _open_state_db_for(settings: Settings) -> sqlite3.Connection:
    """Open the spec-§4 state DB. Centralised so tests can monkeypatch
    if they need to redirect production-path access."""
    return open_state_db(db_path=settings.db_path, backup_dir=settings.backup_dir)


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
