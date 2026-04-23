"""whatsbot configuration loaded once at startup.

Quelle: Environment-Variablen (`WHATSBOT_ENV`, `WHATSBOT_DRY_RUN`) +
hardcoded Pfade aus Spec §4. Secrets selbst werden NICHT hier gehalten —
sie kommen via `SecretsProvider` aus dem macOS Keychain.

Drei Environments (Spec §17 Test-Strategie + §22 Deploy):
    prod  → Bot läuft als LaunchAgent, Webhook-Sigcheck strikt, alle 7 Secrets
            müssen vorhanden sein (sonst harter Abbruch).
    dev   → ``make run-dev``, Webhook-Sigcheck umgangen (Spec §17), fehlende
            Secrets werden als Warning geloggt aber stoppen den Start nicht
            — sinnvoll vor dem ersten ``make setup-secrets``.
    test  → pytest. Secrets werden gar nicht geprüft, Tests injizieren ihre
            eigenen Mocks.
"""

from __future__ import annotations

import os
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

from whatsbot.ports.secrets_provider import SecretsProvider, verify_all_present


class Environment(StrEnum):
    PROD = "prod"
    DEV = "dev"
    TEST = "test"


_DEFAULT_LOG_DIR = Path.home() / "Library" / "Logs" / "whatsbot"
_DEFAULT_DB_PATH = Path.home() / "Library" / "Application Support" / "whatsbot" / "state.db"
_DEFAULT_BACKUP_DIR = Path.home() / "Backups" / "whatsbot"
# Spec §4 — touch-files used by the watchdog (Phase 6). They live in
# /tmp on purpose: a reboot wipes them, an orphaned PANIC marker
# never survives across power cycles.
_DEFAULT_PANIC_MARKER = Path("/tmp/whatsbot-PANIC")  # noqa: S108 — Spec §4 path
_DEFAULT_HEARTBEAT_PATH = Path("/tmp/whatsbot-heartbeat")  # noqa: S108 — Spec §4 path


class Settings(BaseModel):
    """Runtime configuration. Stable for the lifetime of one process."""

    env: Environment = Environment.PROD
    dry_run: bool = False
    log_dir: Path = Field(default_factory=lambda: _DEFAULT_LOG_DIR)
    db_path: Path = Field(default_factory=lambda: _DEFAULT_DB_PATH)
    backup_dir: Path = Field(default_factory=lambda: _DEFAULT_BACKUP_DIR)
    panic_marker_path: Path = Field(default_factory=lambda: _DEFAULT_PANIC_MARKER)
    heartbeat_path: Path = Field(default_factory=lambda: _DEFAULT_HEARTBEAT_PATH)
    bind_host: str = "127.0.0.1"
    bind_port: int = 8000
    hook_bind_host: str = "127.0.0.1"
    hook_bind_port: int = 8001

    @classmethod
    def from_env(cls) -> Settings:
        env_raw = os.environ.get("WHATSBOT_ENV", "prod").strip().lower()
        try:
            env = Environment(env_raw)
        except ValueError as exc:
            raise InvalidEnvironmentError(
                f"WHATSBOT_ENV={env_raw!r} ungueltig. "
                f"Erlaubt: {', '.join(e.value for e in Environment)}."
            ) from exc
        dry_run = os.environ.get("WHATSBOT_DRY_RUN", "0").strip() in {"1", "true", "yes"}
        return cls(env=env, dry_run=dry_run)


class InvalidEnvironmentError(ValueError):
    """Raised when WHATSBOT_ENV is not one of {prod, dev, test}."""


class SecretsValidationError(RuntimeError):
    """Raised in prod when one or more required Keychain secrets are missing."""


def assert_secrets_present(
    provider: SecretsProvider,
    settings: Settings,
) -> list[str]:
    """Return the list of missing secret keys.

    In ``prod`` env, missing secrets are a fatal startup error (raises
    ``SecretsValidationError``). In ``dev`` and ``test``, the list is just
    returned — the caller (typically ``main.create_app``) decides what to do
    (usually: log a warning and proceed).
    """
    missing = verify_all_present(provider)
    if missing and settings.env is Environment.PROD:
        raise SecretsValidationError(
            f"Pflicht-Secrets fehlen im Keychain: {', '.join(missing)}. "
            f"Setze sie via `make setup-secrets`."
        )
    return missing
