"""whatsbot FastAPI application factory.

Uvicorn-Aufruf via Factory:
    uvicorn whatsbot.main:create_app --factory ...

So bekommen Tests eine eigene App via ``create_app(Settings(env=TEST), ...)``
ohne dass beim Import ein Side-Effect-Setup läuft.
"""

from __future__ import annotations

import time
from typing import Final

from fastapi import FastAPI
from fastapi.responses import PlainTextResponse

import whatsbot
from whatsbot.adapters.keychain_provider import KeychainProvider
from whatsbot.config import Environment, Settings, assert_secrets_present
from whatsbot.http.middleware import CorrelationIdMiddleware
from whatsbot.logging_setup import configure_logging, get_logger
from whatsbot.ports.secrets_provider import SecretsProvider

_started_at_monotonic: Final[float] = time.monotonic()


def create_app(
    settings: Settings | None = None,
    secrets_provider: SecretsProvider | None = None,
) -> FastAPI:
    """Build a fresh FastAPI app. Single entry point for prod, dev and tests."""
    settings = settings if settings is not None else Settings.from_env()

    configure_logging(
        log_dir=settings.log_dir,
        write_to_files=settings.env is not Environment.TEST,
    )
    log = get_logger("whatsbot.startup")

    if settings.env is not Environment.TEST:
        provider: SecretsProvider = (
            secrets_provider if secrets_provider is not None else KeychainProvider()
        )
        missing = assert_secrets_present(provider, settings)
        if missing:
            # In dev: the assertion does not raise — we only warn so the user
            # can fire up the server before running ``make setup-secrets``.
            log.warning(
                "secrets_missing_dev_mode",
                env=settings.env.value,
                missing=missing,
            )

    app = FastAPI(
        title="whatsbot",
        version=whatsbot.__version__,
        description="Persoenlicher WhatsApp-Bot zur Fernsteuerung von Claude Code (single user).",
    )
    app.add_middleware(CorrelationIdMiddleware)

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
